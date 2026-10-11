"""MAC address vendors from the IEEE registry (MA-L, MA-M and MA-S blocks),
downloaded on demand into the user's data folder and never bundled -- the
same arrangement as the Bluetooth SIG names (msdr/ble/sig.py). Used for
Wi-Fi BSSIDs and BLE public addresses (docs/wifi.md "Vendor names").

Only prefix -> organization name is kept; the registry's street addresses
are dropped. A locally administered address (bit 0x02 of the first byte:
guest/mesh SSIDs a router derives from its own MAC, or a random MAC) is not
from the registry, so it never gets a vendor.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

SOURCES = {
    "MA-L": "https://standards-oui.ieee.org/oui/oui.csv",
    "MA-M": "https://standards-oui.ieee.org/oui28/mam.csv",
    "MA-S": "https://standards-oui.ieee.org/oui36/oui36.csv",
}
FETCH_TIMEOUT_S = 60
# Most specific block first: an MA-S/MA-M assignment sits inside a larger
# MA-L block owned by someone else (often the IEEE itself).
_PREFIX_LENGTHS = (9, 7, 6)


class OuiNames:
    def __init__(self, prefixes: dict[str, str], fetched_at: float | None) -> None:
        self.prefixes = prefixes  # uppercase hex prefix (6, 7 or 9 digits) -> name
        self.fetched_at = fetched_at

    def summary(self) -> dict:
        return {"prefixes": len(self.prefixes), "fetched_at": self.fetched_at}


_names: OuiNames | None = None


def names() -> OuiNames | None:
    return _names


def set_names(value: OuiNames | None) -> None:
    global _names
    _names = value


def _hex(mac: str) -> str:
    return "".join(c for c in mac if c not in ":-.").upper()


def is_locally_administered(mac: str) -> bool:
    digits = _hex(mac)
    return len(digits) >= 2 and bool(int(digits[:2], 16) & 0x02)


def vendor(mac: str | None) -> str | None:
    """Registry organization for `mac`, or None (no download, locally
    administered, or unassigned)."""
    if not mac or _names is None or is_locally_administered(mac):
        return None
    digits = _hex(mac)
    for n in _PREFIX_LENGTHS:
        name = _names.prefixes.get(digits[:n])
        if name:
            return name
    return None


def parse(sources: dict[str, str]) -> OuiNames:
    """`sources` maps SOURCES keys to each CSV's text (columns Registry,
    Assignment, Organization Name, Organization Address)."""
    prefixes: dict[str, str] = {}
    for text in sources.values():
        for row in csv.DictReader(io.StringIO(text)):
            assignment = (row.get("Assignment") or "").strip().upper()
            name = (row.get("Organization Name") or "").strip()
            if assignment and name:
                prefixes[assignment] = name
    return OuiNames(prefixes, fetched_at=time.time())


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "msdr"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", errors="replace")


def refresh(path: Path, fetch: Callable[[str], str] = _fetch) -> OuiNames:
    """Download all three registries, write prefix -> name JSON to `path`
    atomically (a failed download keeps the previous file), activate it."""
    loaded = parse({key: fetch(url) for key, url in SOURCES.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": loaded.fetched_at, "prefixes": loaded.prefixes}, fh)
    os.replace(tmp, path)
    set_names(loaded)
    return loaded


def load(path: Path) -> OuiNames | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        loaded = OuiNames(dict(data["prefixes"]), data.get("fetched_at"))
    except (OSError, ValueError, KeyError, TypeError):
        loaded = None
    set_names(loaded)
    return loaded
