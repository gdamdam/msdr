"""Import the frequency allocation dataset (docs/allocations.md).

Band edges and services come from the NTIA Redbook chapter 4 HTML grid,
footnote prose from 47 CFR §2.106 via the eCFR versioner API. The NTIA
footnote page is fetched only to detect footnote-text disagreements.

    python -m msdr.allocations.importer --out msdr/allocations/data/dataset.json
    python -m msdr.allocations.importer --check msdr/allocations/data/dataset.json
    python -m msdr.allocations.importer --from-file normalized.json --out out.json

Never called per lookup; the store only reads the JSON this writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Any

from .model import (
    COLUMNS,
    IMPORTER_VERSION,
    PRIORITIES,
    SCHEMA_VERSION,
    Band,
    Footnote,
    Service,
    SourceStatus,
    footnote_kind,
    is_footnote_id,
)

NTIA_TABLE_URL = "https://redbook.ntia.gov/json/preRenderedTableOfAllocations-from-xml.html"
NTIA_FOOTNOTES_URL = "https://redbook.ntia.gov/json/preRenderedChapter4Footnotes.html"
NTIA_FOOTNOTES_TITLE = "NTIA Manual ch. 4 footnotes (fallback text for ids absent from eCFR)"
NTIA_EDITION = "2025"
NTIA_TITLE = (
    "NTIA Manual of Regulations and Procedures for Federal Radio Frequency Management, ch. 4"
)
ECFR_VERSIONS_URL = (
    "https://www.ecfr.gov/api/versioner/v1/versions/title-47.json?part=2&section={section}"
)
ECFR_FULL_URL = (
    "https://www.ecfr.gov/api/versioner/v1/full/{date}/title-47.xml?part=2&section={section}"
)
ECFR_TITLE = "47 CFR §2.106 Table of Frequency Allocations (footnotes)"
ECFR_PAGE_URL = "https://www.ecfr.gov/current/title-47/section-2.106"

# Expected extent of the NTIA table: the grid starts at the "0-8.3 kHz" row
# and ends at "275-3000 GHz". Both are checked, not assumed.
TABLE_LO_HZ = 0
TABLE_HI_HZ = 3_000_000_000_000
# Floor for a full import across the five columns (the 2025 table yields ~2670).
MIN_BANDS = 2000

USER_AGENT = "msdr-allocations/" + IMPORTER_VERSION
_UNIT_HZ = {"Hz": 1, "kHz": 1_000, "MHz": 1_000_000, "GHz": 1_000_000_000}
_GRID_COLUMNS = {1: "itu_r1", 2: "itu_r2", 3: "itu_r3", 4: "us_federal", 5: "us_non_federal"}
_RULE_PART_GRID_COLUMN = 6


class AllocationImportError(Exception):
    """Raised when a source cannot be fetched/parsed or validation fails."""


def _utcnow() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- fetching


@dataclass
class Fetched:
    url: str
    data: bytes
    sha256: str
    size: int
    fetched_at: str
    last_modified: str | None = None


def fetch_bytes(
    url: str, *, accept_gzip: bool = False, timeout: float = 120, retries: int = 30
) -> Fetched:
    """GET `url` fully, resuming with Range requests when the connection is cut.

    Both publishers close long transfers early through some proxies, so an
    IncompleteRead is treated as "resume from here" rather than a failure.
    With gzip the resume offsets refer to the compressed representation, which
    is what Range addresses; decompression happens once at the end.
    """
    buf = bytearray()
    encoding: str | None = None
    last_modified: str | None = None
    for attempt in range(retries):
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        headers["Accept-Encoding"] = "gzip" if accept_gzip else "identity"
        if buf:
            headers["Range"] = f"bytes={len(buf)}-"
        req = urllib.request.Request(url, headers=headers)
        total: int | None = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200 and buf:
                    buf = bytearray()  # server ignored Range: start over
                encoding = resp.headers.get("Content-Encoding") or encoding
                last_modified = resp.headers.get("Last-Modified") or last_modified
                content_range = resp.headers.get("Content-Range")
                # "bytes 0-9/*" (unknown total) falls through to Content-Length.
                if (
                    content_range
                    and "/" in content_range
                    and content_range.rsplit("/", 1)[1].strip().isdigit()
                ):
                    total = int(content_range.rsplit("/", 1)[1])
                elif resp.headers.get("Content-Length"):
                    total = int(resp.headers["Content-Length"]) + (
                        len(buf) if resp.status == 206 else 0
                    )
                try:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        buf += chunk
                except http.client.IncompleteRead as exc:
                    buf += exc.partial
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and buf:
                break
            raise AllocationImportError(f"HTTP {exc.code} fetching {url}") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            if attempt == retries - 1:
                raise AllocationImportError(f"could not fetch {url}: {exc}") from exc
            time.sleep(min(2.0 * (attempt + 1), 10.0))
            continue
        if total is None or len(buf) >= total:
            break
        time.sleep(1.0)
    else:
        raise AllocationImportError(
            f"gave up fetching {url} after {retries} attempts ({len(buf)} bytes)"
        )
    data = bytes(buf)
    if encoding == "gzip":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error) as exc:
            # EOFError = truncated stream, zlib.error = corrupt data; neither
            # is an OSError subclass.
            raise AllocationImportError(f"truncated or corrupt gzip body from {url}") from exc
    return Fetched(
        url=url,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        fetched_at=_utcnow(),
        last_modified=last_modified,
    )


def fetch_ntia_table(url: str = NTIA_TABLE_URL) -> Fetched:
    return fetch_bytes(url)


def fetch_ntia_footnotes(url: str = NTIA_FOOTNOTES_URL) -> Fetched:
    return fetch_bytes(url)


def fetch_ecfr_versions(section: str = "2.106") -> list[dict[str, Any]]:
    """Amendment history of the section from the versioner API, oldest first."""
    fetched = fetch_bytes(ECFR_VERSIONS_URL.format(section=section), accept_gzip=True)
    try:
        payload = json.loads(fetched.data)
    except ValueError as exc:
        raise AllocationImportError("eCFR versions endpoint returned invalid JSON") from exc
    versions = [v for v in payload.get("content_versions", []) if v.get("identifier") == section]
    if not versions:
        raise AllocationImportError(f"eCFR versions endpoint lists no versions for §{section}")
    return sorted(versions, key=lambda v: v.get("date", ""))


def fetch_ecfr_section(date: str | None = None, section: str = "2.106") -> tuple[Fetched, str]:
    """Return the section XML and the amendment date it was fetched for.

    Without `date` the latest amendment date from the versions endpoint is
    used; that date is the dataset's `effective_date` for `ecfr_2_106`.
    """
    if date is None:
        date = fetch_ecfr_versions(section)[-1]["date"]
    fetched = fetch_bytes(ECFR_FULL_URL.format(date=date, section=section), accept_gzip=True)
    return fetched, date


# --------------------------------------------------------------------------- NTIA grid


_BAND_LABEL_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)\s*(kHz|MHz|GHz|Hz)\s*$"
)
_GRID_POS_RE = re.compile(r"grid-(column|row)\s*:\s*(\d+)(?:\s*/\s*(\d+))?")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_QUALIFIER_START_RE = re.compile(r"\(|\bexcept\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _hz(value: str, unit: str) -> int:
    scaled = Decimal(value) * _UNIT_HZ[unit]
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{value} {unit} is not a whole number of Hz")
    return int(scaled)


def parse_band_label(label: str) -> tuple[int, int]:
    m = _BAND_LABEL_RE.match(label)
    if not m:
        raise ValueError(f"unrecognised band label {label!r}")
    try:
        lo, hi = _hz(m.group(1), m.group(3)), _hz(m.group(2), m.group(3))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"unparseable band label {label!r}: {exc}") from exc
    if hi <= lo:
        raise ValueError(f"band label {label!r} has hi <= lo")
    return lo, hi


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\xa0", " ")).strip()


def _split_footnote_group(group: str) -> list[str] | None:
    tokens = [t.strip() for t in group.split(",")]
    if tokens and all(t and is_footnote_id(t) for t in tokens):
        return tokens
    return None


def parse_service_text(text: str) -> Service | None:
    """One grid line -> Service, or None for "NOT ALLOCATED".

    ALL CAPS name = primary, mixed case = secondary. Parenthesised groups
    made only of footnote ids are footnotes of this service; every other
    parenthesis and anything from "except" on is the qualifier.
    """
    raw = _clean(text)
    if not raw:
        return None
    if raw.strip("()").upper() == "NOT ALLOCATED":
        return None
    footnotes: list[str] = []

    def _take(m: re.Match[str]) -> str:
        ids = _split_footnote_group(m.group(1))
        if ids is None:
            return m.group(0)
        footnotes.extend(ids)
        return " "

    rest = _clean(_PAREN_RE.sub(_take, raw))
    # Defensive: bare trailing ids such as "MOBILE 5.132A" (not seen in the
    # 2025 grid, which parenthesises them, but cheap to accept).
    words = rest.split(" ")
    while words and is_footnote_id(words[-1].rstrip(",")):
        footnotes.append(words.pop().rstrip(","))
    rest = " ".join(words)
    m = _QUALIFIER_START_RE.search(rest)
    if m:
        name, qualifier = rest[: m.start()].strip(), rest[m.start() :].strip()
    else:
        name, qualifier = rest, ""
    if not name:
        raise ValueError(f"no service name in {raw!r}")
    qualifier = re.sub(r"\)\s*\(", ") (", qualifier)
    if qualifier[:6].lower() == "except":
        # The grid title-cases the clause ("Except Aeronautical Mobile"); the
        # Regulations write it in lower case. Parenthesised parts keep case.
        head, sep, tail = qualifier.partition("(")
        qualifier = head.lower() + sep + tail
    priority = "primary" if name.upper() == name else "secondary"
    return Service(
        name=name,
        priority=priority,
        qualifier=qualifier or None,
        footnotes=footnotes,
        raw=raw,
    )


@dataclass
class _Cell:
    cell_id: str
    col0: int = 0
    col1: int = 0
    row0: int = 0
    row1: int = 0
    label: str | None = None
    service_lines: list[str] = field(default_factory=list)
    footnote_lines: list[str] = field(default_factory=list)


class _GridParser(HTMLParser):
    """Collect `.allocation-cell` blocks: grid position plus their text lines.

    Each cell holds a flex wrapper with: a bold band label (absent in the FCC
    rule-part column), one div per service line, and a final `margin-top:
    auto` div holding the cell-level footnotes.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[_Cell] = []
        self._cell: _Cell | None = None
        self._depth = 0
        self._line_style: str | None = None
        self._line_depth = 0
        self._line_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div":
            return
        a = dict(attrs)
        if self._cell is None:
            if a.get("class") == "allocation-cell":
                cell = _Cell(cell_id=a.get("data-allocation-id") or f"anon-{len(self.cells)}")
                for kind, start, end in _GRID_POS_RE.findall(a.get("style") or ""):
                    s = int(start)
                    e = int(end) if end else s + 1
                    if kind == "column":
                        cell.col0, cell.col1 = s, e
                    else:
                        cell.row0, cell.row1 = s, e
                self._cell = cell
                self._depth = 1
            return
        self._depth += 1
        self._flush_line()
        self._line_style = a.get("style") or ""
        self._line_depth = self._depth
        self._line_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "div" or self._cell is None:
            return
        if self._line_style is not None and self._depth == self._line_depth:
            self._flush_line()
        self._depth -= 1
        if self._depth == 0:
            self.cells.append(self._cell)
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and self._line_style is not None:
            self._line_text.append(data)

    def _flush_line(self) -> None:
        if self._line_style is None:
            return
        text = _clean("".join(self._line_text))
        style = self._line_style
        self._line_style = None
        self._line_text = []
        cell = self._cell
        if cell is None or not text:
            return
        if "font-weight: bold" in style:
            cell.label = text
        elif "margin-top: auto" in style:
            cell.footnote_lines.append(text)
        else:
            cell.service_lines.append(text)


def parse_ntia_table(html: str, problems: list[dict[str, Any]] | None = None) -> list[Band]:
    """Parse the NTIA grid into one Band per (cell, column).

    Cells the parser cannot handle are appended to `problems` (with their
    data-allocation-id) and skipped; the contiguity check in `validate`
    then reports the hole they leave.
    """
    parser = _GridParser()
    parser.feed(html)
    parser.close()
    if problems is None:
        problems = []
    bands: list[Band] = []
    rule_cells: list[_Cell] = []
    for cell in parser.cells:
        if cell.col0 == _RULE_PART_GRID_COLUMN:
            rule_cells.append(cell)
            continue
        if cell.label is None:
            problems.append({"cell_id": cell.cell_id, "error": "no band label"})
            continue
        try:
            lo, hi = parse_band_label(cell.label)
        except ValueError as exc:
            problems.append({"cell_id": cell.cell_id, "error": str(exc)})
            continue
        services: list[Service] = []
        unallocated = False
        bad = False
        for line in cell.service_lines:
            try:
                svc = parse_service_text(line)
            except ValueError as exc:
                problems.append({"cell_id": cell.cell_id, "error": str(exc)})
                bad = True
                break
            if svc is None:
                unallocated = True
            else:
                services.append(svc)
        if bad:
            continue
        cell_footnotes: list[str] = []
        for line in cell.footnote_lines:
            ids = _split_footnote_group(line)
            if ids is None:
                problems.append(
                    {"cell_id": cell.cell_id, "error": f"unrecognised footnote line {line!r}"}
                )
                bad = True
                break
            cell_footnotes.extend(ids)
        if bad:
            continue
        if not services:
            unallocated = True  # empty by rule (e.g. Federal column at 535-1605 kHz)
        for grid_col in range(cell.col0, cell.col1):
            column = _GRID_COLUMNS.get(grid_col)
            if column is None:
                problems.append(
                    {"cell_id": cell.cell_id, "error": f"grid column {grid_col} unknown"}
                )
                continue
            bands.append(
                _band_with_rows(lo, hi, column, services, cell_footnotes, unallocated, cell)
            )
    _attach_rule_parts(bands, rule_cells)
    return bands


_ROWS_ATTR = "_grid_rows"


def _band_with_rows(
    lo: int,
    hi: int,
    column: str,
    services: list[Service],
    cell_footnotes: list[str],
    unallocated: bool,
    cell: _Cell,
) -> Band:
    band = Band(
        lo_hz=lo,
        hi_hz=hi,
        column=column,
        services=[Service(**s.to_dict()) for s in services],
        footnotes=list(cell_footnotes),
        unallocated=unallocated,
        cell_id=cell.cell_id,
    )
    # Grid rows are only needed to join the rule-part column; not serialised.
    setattr(band, _ROWS_ATTR, (cell.row0, cell.row1))
    return band


def _attach_rule_parts(bands: list[Band], rule_cells: list[_Cell]) -> None:
    """The FCC rule-part column has no band labels: join by grid-row overlap."""
    for band in bands:
        if band.column != "us_non_federal":
            continue
        r0, r1 = getattr(band, _ROWS_ATTR, (0, 0))
        parts: list[str] = []
        for cell in rule_cells:
            if cell.row0 < r1 and cell.row1 > r0:
                for line in cell.service_lines:
                    if line not in parts:
                        parts.append(line)
        band.rule_parts = parts
    for band in bands:
        if hasattr(band, _ROWS_ATTR):
            delattr(band, _ROWS_ATTR)


# --------------------------------------------------------------------------- footnotes


_NTIA_FOOTNOTE_SPLIT_RE = re.compile(r'data-footnote-id="toa_([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")


def parse_ntia_footnotes(html: str) -> dict[str, str]:
    """NTIA chapter 4 footnote page -> {id: plain text} (comparison only)."""
    import html as html_mod

    out: dict[str, str] = {}
    matches = list(_NTIA_FOOTNOTE_SPLIT_RE.finditer(html))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        # The id sits inside the opening <div ...>: skip to its ">" and stop
        # before the "<div" that carries the next id.
        tag_end = html.find(">", m.end())
        if i + 1 < len(matches):
            end = html.rfind("<", m.end(), end)
        chunk = html[tag_end + 1 : end] if 0 <= tag_end < end else html[m.end() : end]
        text = _clean(html_mod.unescape(_TAG_RE.sub(" ", chunk)))
        fid = m.group(1)
        if text.startswith(fid):
            text = text[len(fid) :].strip()
        out[fid] = text
    return out


_ECFR_START_RE = re.compile(
    r"^(?:\([0-9ivxlcA-Z]+\)\s*){0,2}(5\.\d+[A-Z]*|US\d+[A-Z]*|NG\d+[A-Z]*|G\d+[A-Z]*)\.?\s+(.*)$",
    re.S,
)
_ECFR_SECTION_RE = re.compile(r"^\(([b-e])\)\s")


def _element_text(el: ET.Element) -> str:
    if el.tag == "TABLE":
        rows = []
        for tr in el.iter("TR"):
            cells = [_clean("".join(td.itertext())) for td in tr if td.tag in ("TD", "TH")]
            rows.append(" | ".join(c for c in cells if c))
        return "\n".join(r for r in rows if r)
    return _clean("".join(el.itertext()))


def parse_ecfr_footnotes(xml: str | bytes) -> list[Footnote]:
    """§2.106 paragraphs (b)-(e): one Footnote per "(n) ID text" paragraph.

    Continuation paragraphs, notes and tables that follow a footnote are
    appended to its text until the next footnote (or section) starts.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise AllocationImportError(f"eCFR XML does not parse: {exc}") from exc
    footnotes: list[Footnote] = []
    current: Footnote | None = None
    in_footnotes = False
    skip: set[int] = set()
    for el in root.iter():
        if id(el) in skip:
            continue
        if el.tag == "TABLE":
            for child in el.iter():
                skip.add(id(child))
            if current is not None:
                current.text = (current.text or "") + "\n" + _element_text(el)
            continue
        if el.tag not in ("P", "HD", "HEAD", "NOTE", "EDNOTE"):
            continue
        if el.tag in ("NOTE", "EDNOTE"):
            for child in el.iter():
                skip.add(id(child))
        text = _element_text(el)
        if not text:
            continue
        sec = _ECFR_SECTION_RE.match(text)
        if sec:
            in_footnotes = True
            current = None  # paragraph intro, not footnote prose
            continue
        if not in_footnotes:
            continue
        if "[Reserved]" in text and len(text) < 40:
            continue
        m = _ECFR_START_RE.match(text)
        if m:
            current = Footnote(
                id=m.group(1), text=m.group(2).strip(), kind=footnote_kind(m.group(1))
            )
            footnotes.append(current)
        elif current is not None:
            current.text = (current.text or "") + "\n" + text
    if not footnotes:
        raise AllocationImportError("no footnotes found in eCFR §2.106 XML")
    return footnotes


_WRC_RE = re.compile(r"\(wrc[\u2010-\u2015-]*\d+\)")
# Citation style differs systematically between eCFR ("§ 87.149 of this
# chapter", "paragraph (b)(169)(i) of this section", sub-paragraphs (i)/(ii))
# and the Redbook ("47 CFR 87.149", "No. 5.169", sub-paragraphs (a)/(b)).
_CITATION_NOISE_RE = re.compile(r"ofthischapter|ofthissection|47c\.?f\.?r\.?|§+|\bnos?\.")
_PAREN_MARKER_RE = re.compile(r"\([a-z0-9]{1,4}\)")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_footnote_text(text: str) -> str:
    """Comparison key for footnote prose.

    Whitespace is removed first because the Redbook HTML letter-spaces some
    words ("i n Re g ion"); then case, punctuation, WRC markers, short
    paragraph markers and the citation style are dropped so that only
    wording differences count as disagreements.
    """
    t = re.sub(r"\s+", "", text.lower())
    t = _WRC_RE.sub("", t)
    t = _CITATION_NOISE_RE.sub("", t)
    t = _PAREN_MARKER_RE.sub("", t)
    return _NONALNUM_RE.sub("", t)


def find_disagreements(
    footnotes: list[Footnote], ntia_texts: dict[str, str]
) -> list[dict[str, Any]]:
    """Footnotes whose normalised eCFR and NTIA texts differ. Recorded, not resolved."""
    out = []
    for fn in footnotes:
        other = ntia_texts.get(fn.id)
        if fn.text is None or other is None:
            continue
        if normalize_footnote_text(fn.text) != normalize_footnote_text(other):
            out.append(
                {"kind": "footnote_text", "id": fn.id, "sources": ["ntia_table", "ecfr_2_106"]}
            )
    return out


# --------------------------------------------------------------------------- validation


def _fmt_hz(hz: int) -> str:
    for unit, div in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if hz >= div:
            return f"{hz / div:g} {unit}"
    return f"{hz} Hz"


def validate(
    bands: list[Band],
    footnotes: list[Footnote],
    *,
    min_bands: int = 1,
    lo_hz: int | None = None,
    hi_hz: int | None = None,
) -> list[str]:
    """Return every problem found (empty list = valid).

    Each column must be contiguous (no gaps, no overlaps) over its own extent,
    and over [lo_hz, hi_hz] when given; every referenced footnote id must be
    present in `footnotes` (a `missing: true` placeholder counts, dropping
    the id does not); every service needs a known priority; at least
    `min_bands` rows overall.
    """
    errors: list[str] = []
    if len(bands) < min_bands:
        errors.append(f"only {len(bands)} bands, floor is {min_bands}")
    known = {f.id for f in footnotes}
    by_col: dict[str, list[Band]] = {}
    for b in bands:
        if b.column not in COLUMNS:
            errors.append(f"unknown column {b.column!r} at {_fmt_hz(b.lo_hz)}-{_fmt_hz(b.hi_hz)}")
            continue
        if b.hi_hz <= b.lo_hz:
            errors.append(f"{b.column}: empty or inverted band {b.lo_hz}-{b.hi_hz} Hz")
        by_col.setdefault(b.column, []).append(b)
        for s in b.services:
            if s.priority not in PRIORITIES:
                errors.append(
                    f"{b.column} {_fmt_hz(b.lo_hz)}-{_fmt_hz(b.hi_hz)}: service {s.raw!r} "
                    f"has unknown priority {s.priority!r}"
                )
            if not s.name:
                errors.append(
                    f"{b.column} {_fmt_hz(b.lo_hz)}-{_fmt_hz(b.hi_hz)}: service without a name"
                )
        if not b.services and not b.unallocated:
            errors.append(
                f"{b.column} {_fmt_hz(b.lo_hz)}-{_fmt_hz(b.hi_hz)}: "
                "no services but not marked unallocated"
            )
        for fid in b.footnote_ids():
            if fid not in known:
                errors.append(
                    f"{b.column} {_fmt_hz(b.lo_hz)}-{_fmt_hz(b.hi_hz)}: footnote {fid} unresolved"
                )
    for col, rows in by_col.items():
        rows.sort(key=lambda b: (b.lo_hz, b.hi_hz))
        if lo_hz is not None and rows[0].lo_hz != lo_hz:
            errors.append(f"{col}: starts at {_fmt_hz(rows[0].lo_hz)}, expected {_fmt_hz(lo_hz)}")
        if hi_hz is not None and rows[-1].hi_hz != hi_hz:
            errors.append(f"{col}: ends at {_fmt_hz(rows[-1].hi_hz)}, expected {_fmt_hz(hi_hz)}")
        for prev, cur in zip(rows, rows[1:], strict=False):
            if cur.lo_hz > prev.hi_hz:
                errors.append(f"{col}: gap {_fmt_hz(prev.hi_hz)}-{_fmt_hz(cur.lo_hz)}")
            elif cur.lo_hz < prev.hi_hz:
                errors.append(
                    f"{col}: overlap {_fmt_hz(prev.lo_hz)}-{_fmt_hz(prev.hi_hz)} with "
                    f"{_fmt_hz(cur.lo_hz)}-{_fmt_hz(cur.hi_hz)}"
                )
    return errors


def unresolved_footnote_ids(bands: list[Band], footnotes: list[Footnote]) -> list[str]:
    known = {f.id for f in footnotes}
    out: list[str] = []
    for b in bands:
        for fid in b.footnote_ids():
            if fid not in known and fid not in out:
                out.append(fid)
    return out


def resolve_missing_footnotes(
    bands: list[Band], footnotes: list[Footnote], ntia_texts: dict[str, str]
) -> tuple[list[Footnote], list[str]]:
    """Fill ids the table references but eCFR lacks from the NTIA footnote page.

    Returns (fallback footnotes flagged `fallback: true`, ids still unresolved
    anywhere). eCFR text is never replaced by this.
    """
    fallbacks: list[Footnote] = []
    still: list[str] = []
    for fid in unresolved_footnote_ids(bands, footnotes):
        text = ntia_texts.get(fid)
        if text:
            fallbacks.append(
                Footnote(
                    id=fid,
                    text=text,
                    source_id="ntia_footnotes",
                    kind=footnote_kind(fid),
                    fallback=True,
                )
            )
        else:
            still.append(fid)
    return fallbacks, still


# --------------------------------------------------------------------------- dataset


def build_dataset(
    bands: list[Band],
    footnotes: list[Footnote],
    sources: list[SourceStatus],
    *,
    disagreements: list[dict[str, Any]] | None = None,
    unresolved: list[str] | None = None,
    parser_problems: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utcnow(),
        "sources": [s.to_dict() for s in sources],
        "bands": [b.to_dict() for b in bands],
        "footnotes": [f.to_dict() for f in footnotes],
        "disagreements": list(disagreements or []),
        "unresolved_footnotes": list(unresolved or []),
        "parser_problems": list(parser_problems or []),
    }


def dataset_records(
    dataset: dict[str, Any],
) -> tuple[list[Band], list[Footnote], list[SourceStatus]]:
    """Decode a dataset dict; raises AllocationImportError on structural problems."""
    if not isinstance(dataset, dict):
        raise AllocationImportError("dataset is not a JSON object")
    if dataset.get("schema_version") != SCHEMA_VERSION:
        raise AllocationImportError(
            f"schema_version {dataset.get('schema_version')!r} is not {SCHEMA_VERSION}"
        )
    try:
        bands = [Band.from_dict(b) for b in dataset.get("bands") or []]
        footnotes = [Footnote.from_dict(f) for f in dataset.get("footnotes") or []]
        sources = [SourceStatus.from_dict(s) for s in dataset.get("sources") or []]
    except (KeyError, TypeError, ValueError) as exc:
        raise AllocationImportError(f"malformed record: {exc!r}") from exc
    if not bands:
        raise AllocationImportError("dataset has no bands")
    return bands, footnotes, sources


def validate_dataset(
    dataset: dict[str, Any],
    *,
    min_bands: int = 1,
    known_footnotes: list[Footnote] | None = None,
) -> list[str]:
    """Structural decode plus `validate`; column extents must match the band source.

    `known_footnotes` (e.g. the bundled dataset's) also count as resolved, so
    an overlay that only adds columns need not repeat the footnote texts.
    """
    try:
        bands, footnotes, sources = dataset_records(dataset)
    except AllocationImportError as exc:
        return [str(exc)]
    errors = validate(bands, footnotes + list(known_footnotes or []), min_bands=min_bands)
    by_source: dict[str, list[Band]] = {}
    for b in bands:
        by_source.setdefault(b.source_id, []).append(b)
    for src in sources:
        rows = by_source.get(src.id)
        if rows is None:
            continue
        lo, hi = min(b.lo_hz for b in rows), max(b.hi_hz for b in rows)
        if src.lo_hz is not None and src.lo_hz != lo:
            errors.append(f"source {src.id}: declares lo_hz {src.lo_hz}, bands start at {lo}")
        if src.hi_hz is not None and src.hi_hz != hi:
            errors.append(f"source {src.id}: declares hi_hz {src.hi_hz}, bands end at {hi}")
        if src.band_count and src.band_count != len(rows):
            errors.append(
                f"source {src.id}: declares {src.band_count} bands, file holds {len(rows)}"
            )
    return errors


def bundled_dataset_path() -> Path:
    return Path(str(resources.files("msdr.allocations").joinpath("data").joinpath("dataset.json")))


def bundled_footnotes() -> list[Footnote]:
    """Footnotes of the bundled dataset, or [] when it is missing or unreadable."""
    try:
        return dataset_records(load_dataset(bundled_dataset_path()))[1]
    except AllocationImportError:
        return []


def _decode_utf8(data: bytes, what: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AllocationImportError(f"{what} is not valid UTF-8: {exc}") from exc


def load_dataset(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise AllocationImportError(f"{path}: not found") from exc
    except (OSError, ValueError) as exc:
        raise AllocationImportError(f"{path}: {exc}") from exc


def write_atomic(path: Path, dataset: dict[str, Any]) -> None:
    """Write via temp file + fsync + rename; keep the previous file as dataset.prev.json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dataset, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        # Copy rather than rename the old file away: a rename leaves a window
        # (and, on a crash, a lasting state) with no dataset.json at all.
        prev = path.with_name(path.stem + ".prev.json")
        prev_tmp = prev.with_name(prev.name + ".tmp")
        shutil.copyfile(path, prev_tmp)
        os.replace(prev_tmp, prev)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# --------------------------------------------------------------------------- pipelines


def _source_extent(bands: list[Band]) -> tuple[int, int]:
    return min(b.lo_hz for b in bands), max(b.hi_hz for b in bands)


def import_from_network(
    *,
    ntia_url: str = NTIA_TABLE_URL,
    ecfr_date: str | None = None,
    log=print,
) -> dict[str, Any]:
    """Full import: NTIA grid + eCFR footnotes + NTIA footnote cross-check."""
    log(f"fetching {ntia_url}")
    table = fetch_ntia_table(ntia_url)
    log(f"  {table.size} bytes sha256={table.sha256[:16]}... last-modified={table.last_modified}")
    problems: list[dict[str, Any]] = []
    bands = parse_ntia_table(_decode_utf8(table.data, "NTIA table"), problems)
    log(
        f"  parsed {len(bands)} bands from {len({b.cell_id for b in bands})} cells, "
        f"{len(problems)} problems"
    )

    log("fetching eCFR §2.106")
    section, date = fetch_ecfr_section(ecfr_date)
    log(f"  amendment date {date}, {section.size} bytes sha256={section.sha256[:16]}...")
    footnotes = parse_ecfr_footnotes(section.data)
    log(f"  parsed {len(footnotes)} footnotes")

    log(f"fetching {NTIA_FOOTNOTES_URL}")
    ntia_fn = fetch_ntia_footnotes()
    ntia_texts = parse_ntia_footnotes(_decode_utf8(ntia_fn.data, "NTIA footnotes"))
    disagreements = find_disagreements(footnotes, ntia_texts)
    log(f"  {len(ntia_texts)} NTIA footnotes, {len(disagreements)} text disagreements with eCFR")

    fallbacks, unresolved = resolve_missing_footnotes(bands, footnotes, ntia_texts)
    footnotes.extend(fallbacks)
    if fallbacks:
        log(
            f"  {len(fallbacks)} referenced footnotes absent from eCFR, text taken from the NTIA "
            f"footnote page: {', '.join(f.id for f in fallbacks)}"
        )
    for fid in unresolved:
        footnotes.append(
            Footnote(id=fid, text=None, source_id=None, kind=footnote_kind(fid), missing=True)
        )
    if unresolved:
        log(f"  {len(unresolved)} footnotes unresolved anywhere: {', '.join(unresolved)}")

    errors = validate(bands, footnotes, min_bands=MIN_BANDS, lo_hz=TABLE_LO_HZ, hi_hz=TABLE_HI_HZ)
    if errors:
        raise AllocationImportError("validation failed:\n  " + "\n  ".join(errors))
    lo, hi = _source_extent(bands)
    now = _utcnow()
    sources = [
        SourceStatus(
            id="ntia_table",
            title=NTIA_TITLE,
            publisher="NTIA",
            url=ntia_url,
            edition=NTIA_EDITION,
            effective_date=None,
            imported_at=now,
            origin="imported",
            lo_hz=lo,
            hi_hz=hi,
            band_count=len(bands),
            footnote_count=0,
            provenance={
                "fetched_url": table.url,
                "sha256": table.sha256,
                "size": table.size,
                "last_modified": table.last_modified,
                "fetched_at": table.fetched_at,
                "footnotes_url": ntia_fn.url,
                "footnotes_sha256": ntia_fn.sha256,
                "footnotes_size": ntia_fn.size,
                "importer_version": IMPORTER_VERSION,
            },
        ),
        SourceStatus(
            id="ecfr_2_106",
            title=ECFR_TITLE,
            publisher="eCFR (Office of the Federal Register)",
            url=ECFR_PAGE_URL,
            edition=date,
            effective_date=date,
            imported_at=now,
            origin="imported",
            lo_hz=None,
            hi_hz=None,
            band_count=0,
            footnote_count=len([f for f in footnotes if f.source_id == "ecfr_2_106"]),
            provenance={
                "fetched_url": section.url,
                "sha256": section.sha256,
                "size": section.size,
                "fetched_at": section.fetched_at,
                "importer_version": IMPORTER_VERSION,
            },
        ),
        SourceStatus(
            id="ntia_footnotes",
            title=NTIA_FOOTNOTES_TITLE,
            publisher="NTIA",
            url=NTIA_FOOTNOTES_URL,
            edition=NTIA_EDITION,
            effective_date=None,
            imported_at=now,
            origin="imported",
            lo_hz=None,
            hi_hz=None,
            band_count=0,
            footnote_count=len(fallbacks),
            provenance={
                "fetched_url": ntia_fn.url,
                "sha256": ntia_fn.sha256,
                "size": ntia_fn.size,
                "fetched_at": ntia_fn.fetched_at,
                "footnotes_on_page": len(ntia_texts),
                "importer_version": IMPORTER_VERSION,
            },
        ),
    ]
    return build_dataset(
        bands,
        footnotes,
        sources,
        disagreements=disagreements,
        unresolved=unresolved,
        parser_problems=problems,
    )


def import_from_file(path: Path) -> dict[str, Any]:
    """Validate a user-supplied normalized file and wrap it as a dataset.

    The file uses the dataset shape (`bands`, `footnotes`, optional
    `sources`); only per-column contiguity, footnote resolution (against the
    file plus the bundled footnotes) and priorities are enforced, not the
    full-table floor, so a partial table (e.g. ITU RR columns only) is
    accepted. The store then replaces just the columns the file contains.
    """
    path = Path(path)
    raw = load_dataset(path)
    if not isinstance(raw, dict):
        raise AllocationImportError(f"{path}: dataset is not a JSON object")
    if "schema_version" not in raw:
        raw = dict(raw, schema_version=SCHEMA_VERSION)
    try:
        bands, footnotes, sources = dataset_records(raw)
    except AllocationImportError as exc:
        raise AllocationImportError(f"{path}: {exc}") from exc
    # Footnotes resolve against the union of the file's and the bundled
    # dataset's, since the store merges the two per column.
    errors = validate(bands, footnotes + bundled_footnotes())
    if errors:
        raise AllocationImportError(f"{path}: validation failed:\n  " + "\n  ".join(errors))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    now = _utcnow()
    by_source: dict[str, list[Band]] = {}
    for b in bands:
        by_source.setdefault(b.source_id, []).append(b)
    declared = {s.id: s for s in sources}
    out_sources: list[SourceStatus] = []
    for sid, rows in by_source.items():
        lo, hi = _source_extent(rows)
        src = declared.get(sid) or SourceStatus(id=sid, title=sid, publisher="", url=str(path))
        src.lo_hz, src.hi_hz, src.band_count = lo, hi, len(rows)
        src.footnote_count = len([f for f in footnotes if f.source_id == sid])
        src.imported_at, src.origin, src.status, src.error = now, "imported", "ok", None
        src.provenance = dict(
            src.provenance, fetched_url=str(path), sha256=digest, importer_version=IMPORTER_VERSION
        )
        out_sources.append(src)
    for sid, src in declared.items():
        if sid not in by_source:
            src.imported_at, src.origin = now, "imported"
            src.footnote_count = len([f for f in footnotes if f.source_id == sid])
            out_sources.append(src)
    return build_dataset(
        bands, footnotes, out_sources, disagreements=raw.get("disagreements") or []
    )


def summarize(dataset: dict[str, Any]) -> str:
    bands, footnotes, sources = dataset_records(dataset)
    lines = []
    for src in sources:
        lines.append(
            f"{src.id}: {src.status} edition={src.edition} effective={src.effective_date} "
            f"bands={src.band_count} footnotes={src.footnote_count}"
        )
    per_col: dict[str, int] = {}
    for b in bands:
        per_col[b.column] = per_col.get(b.column, 0) + 1
    lines.append("bands per column: " + ", ".join(f"{c}={per_col.get(c, 0)}" for c in COLUMNS))
    missing = [f.id for f in footnotes if f.missing]
    fallback = [f.id for f in footnotes if f.fallback]
    lines.append(
        f"footnotes: {len(footnotes) - len(missing)} resolved "
        f"({len(fallback)} from the NTIA fallback), {len(missing)} missing"
    )
    lines.append(f"disagreements: {len(dataset.get('disagreements') or [])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m msdr.allocations.importer", description=__doc__)
    ap.add_argument("--out", type=Path, help="write the dataset here (atomic replace)")
    ap.add_argument("--ecfr-date", help="eCFR amendment date YYYY-MM-DD (default: latest)")
    ap.add_argument("--ntia-url", default=NTIA_TABLE_URL)
    ap.add_argument(
        "--from-file", type=Path, help="import a normalized JSON file instead of the network"
    )
    ap.add_argument(
        "--check", type=Path, metavar="DATASET", help="validate an existing dataset file only"
    )
    args = ap.parse_args(argv)

    try:
        if args.check is not None:
            dataset = load_dataset(args.check)
            errors = validate_dataset(dataset)
            if errors:
                print(f"{args.check}: INVALID", file=sys.stderr)
                for e in errors:
                    print("  " + e, file=sys.stderr)
                return 1
            print(f"{args.check}: ok")
            print(summarize(dataset))
            return 0
        if args.from_file is not None:
            dataset = import_from_file(args.from_file)
        else:
            dataset = import_from_network(ntia_url=args.ntia_url, ecfr_date=args.ecfr_date)
    except AllocationImportError as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(summarize(dataset))
    if args.out is None:
        print("no --out given; dataset not written", file=sys.stderr)
        return 0
    write_atomic(args.out, dataset)
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
