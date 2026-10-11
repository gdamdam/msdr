"""validation_status(settings) -> dict (S5 plan §2): a pure, read-only
snapshot combining four sources of truth about what msdr's decoders have
actually been shown to do, kept deliberately separate so nothing here can
read as a green "all validated" summary:

1. **Corpus manifest** (tests/corpus/manifest.json): every case an operator
   has reported or a repo fixture -- to be checked, not a confirmed
   identification -- with its intake status and whether its files actually
   exist on disk right now.
2. **Decoder capability matrix** (msdr.decoder.capability_matrix, via
   msdr.decoder.registry.describe -- the same readiness facts GET
   /api/decoders already exposes): validation state, whether that record is
   stale against the installed backend version, and the tests it points at.
3. **Scorecard summary** (docs/validation/scorecard-synthetic.md): the
   synthetic corpus's last-generated timestamp and a count of rows per
   outcome/status, parsed from the committed Markdown -- this module never
   runs a decoder itself.
4. **Outstanding gates**: a fixed list of field-validation work that no
   synthetic fixture or repo test can close, always present regardless of
   how green everything else looks (plan §9: "HF field validation listed as
   unresolved wherever validation status is shown").
"""

import json
import re
from pathlib import Path

import msdr
from msdr.contracts.readiness import DecoderReadiness, Installation, Session
from msdr.decoder.capability_matrix import RDS_ENTRY
from msdr.decoder.registry import describe
from msdr.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_DATA = Path(__file__).parent / "data"
_MANIFEST_PATH = _PACKAGE_DATA / "corpus-manifest.json"
_SCORECARD_PATH = _PACKAGE_DATA / "scorecard-synthetic.md"

# Fixed regardless of what the corpus/decoder/scorecard sections say this
# call -- these are field-validation facts, not something a synthetic test
# run or a repo fixture could ever close (plan §9).
OUTSTANDING_GATES: tuple[str, ...] = (
    "HF field validation: no off-air HF recording is available yet "
    "(wefax/navtex/wwv/dcf77/msf/rtty/packet300/cw are synthetic-fixture "
    "tested only).",
    "Private VHF/UHF captures reported but not accessible from this "
    "environment (tests/corpus/manifest.json: reference_type=private_capture, "
    "status=inaccessible).",
    "Operator-reported VHF/UHF cases not yet checked against an off-air "
    "recording (tests/corpus/manifest.json: status=pending_intake).",
    "AIS field validation pending: the 161.975/162.025 MHz capture is not "
    "accessible; msdr's AIS decode is synthetic-fixture validated only "
    "(tests/corpus/manifest.json id ais-161975-162025).",
    "8-hour soak on the target Mac has not been run (scripts/soak.py ships; "
    "this gate stays open until that run happens on real hardware).",
    "Stereo WFM (FM stereo/pilot decode to two audio channels) is not "
    "implemented; RDS/mono WFM only.",
)


def _corpus_cases() -> list[dict]:
    try:
        manifest = json.loads(_MANIFEST_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    cases = []
    for case in manifest.get("cases", []):
        files = case.get("files") or []
        files_located = (
            all((_REPO_ROOT / f["path"]).is_file() for f in files) if files else None
        )
        cases.append(
            {
                "id": case.get("id"),
                "class": case.get("class"),
                "status": case.get("status"),
                "reference_type": case.get("reference_type"),
                "files_located": files_located,
            }
        )
    return cases


def _rds_readiness() -> DecoderReadiness:
    """RDS (msdr/survey/rds.py) isn't in the DECODERS registry (no manager
    channel, no backend process -- see capability_matrix.py's own note), so
    registry.describe() never lists it; built here from its own RDS_ENTRY so
    validation status still reports it alongside every other decoder."""
    return DecoderReadiness(
        decoder_id="rds",
        installation=Installation(state="builtin", backend="python", version=msdr.__version__),
        validation=RDS_ENTRY.validation,
        session=Session(),
    )


def _decoder_validation(settings: Settings) -> list[dict]:
    entries = describe(settings.decoder.bin_dirs, session=None)
    decoders = [
        {
            "id": entry["id"],
            "label": entry["label"],
            "available": entry["available"],
            "validation_state": entry["readiness"]["validation"]["state"],
            "validation_stale": entry["readiness"]["validation_stale"],
            "tests": entry["readiness"]["validation"]["tests"],
        }
        for entry in entries
    ]
    rds = _rds_readiness().as_api()
    decoders.append(
        {
            "id": "rds",
            "label": "RDS (FM broadcast)",
            "available": True,
            "validation_state": rds["validation"]["state"],
            "validation_stale": rds["validation_stale"],
            "tests": rds["validation"]["tests"],
        }
    )
    return decoders


_GENERATED_RE = re.compile(r"^Generated:\s*(\S+)", re.MULTILINE)


def _table_rows(section: str) -> list[list[str]]:
    """Every `| a | b | ... |` row of a Markdown table in `section`, header
    and `|---|---|` separator rows dropped."""
    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= {"-"} for c in cells):
            continue  # separator row
        if cells and cells[0].lower() in ("case",):
            continue  # header row
        rows.append(cells)
    return rows


def _count_column(section: str, index: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cells in _table_rows(section):
        if len(cells) <= index:
            continue
        key = cells[index]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _scorecard_summary() -> dict:
    try:
        text = _SCORECARD_PATH.read_text()
    except OSError:
        return {
            "path": str(_SCORECARD_PATH),
            "generated_at": None,
            "decoders_by_status": {},
            "detectors_by_outcome": {},
        }
    match = _GENERATED_RE.search(text)
    detectors_section = text.split("## Detectors", 1)[-1].split("## Decoders", 1)[0]
    decoders_section = text.split("## Decoders", 1)[-1].split("## Negative controls", 1)[0]
    return {
        "path": str(_SCORECARD_PATH),
        "generated_at": match.group(1) if match else None,
        # Decoders table: "| case | decoder | status | ... |" -> status is index 2.
        "decoders_by_status": _count_column(decoders_section, 2),
        # Detectors table: "| case | detector | outcome | detail |" -> outcome is index 2.
        "detectors_by_outcome": _count_column(detectors_section, 2),
    }


def validation_status(settings: Settings) -> dict:
    """Pure function: reads the committed manifest/scorecard files and the
    static capability matrix, never runs a decoder or spawns a process."""
    return {
        "corpus_cases": _corpus_cases(),
        "decoders": _decoder_validation(settings),
        "scorecard": _scorecard_summary(),
        "outstanding_gates": list(OUTSTANDING_GATES),
    }
