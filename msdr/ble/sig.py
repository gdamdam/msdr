"""Bluetooth SIG assigned numbers -- company identifiers, 16-bit service
UUIDs (SIG and member) and appearance values -- downloaded on demand into the
user's data folder, never bundled (docs/ble.md "Bluetooth SIG names").

The SIG publishes these lists at bitbucket.org/bluetooth-SIG/public as
proprietary documents that grant no licence, so msdr ships only this
downloader: the operator fetches the lists for their own use under the SIG's
terms, the same way the frequency catalogs are fetched under each source's
terms. Until a download exists, msdr falls back to its short built-in name
tables (msdr/ble/adv.py).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

SOURCE_BASE = "https://bitbucket.org/bluetooth-SIG/public/raw/main/assigned_numbers/"
SOURCE_FILES = {
    "companies": "company_identifiers/company_identifiers.yaml",
    "services": "uuids/service_uuids.yaml",
    "member_services": "uuids/member_uuids.yaml",
    "appearance": "core/appearance_values.yaml",
}
FETCH_TIMEOUT_S = 30


class SigNames:
    def __init__(
        self,
        companies: dict[int, str],
        services: dict[str, str],
        appearance: dict[int, str],
        fetched_at: float | None,
    ) -> None:
        self.companies = companies
        self.services = services  # "0x180f" -> name, lowercase like adv.uuid16
        self.appearance = appearance  # full 16-bit appearance value -> name
        self.fetched_at = fetched_at

    def summary(self) -> dict:
        return {
            "companies": len(self.companies),
            "services": len(self.services),
            "appearance": len(self.appearance),
            "fetched_at": self.fetched_at,
        }

    def to_json(self) -> dict:
        return {
            "fetched_at": self.fetched_at,
            "companies": {str(k): v for k, v in self.companies.items()},
            "services": self.services,
            "appearance": {str(k): v for k, v in self.appearance.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> SigNames:
        return cls(
            companies={int(k): v for k, v in data["companies"].items()},
            services=dict(data["services"]),
            appearance={int(k): v for k, v in data["appearance"].items()},
            fetched_at=data.get("fetched_at"),
        )


_names: SigNames | None = None


def names() -> SigNames | None:
    return _names


def set_names(value: SigNames | None) -> None:
    global _names
    _names = value


def company(company_id: int) -> str | None:
    return _names.companies.get(company_id) if _names else None


def service(uuid: str) -> str | None:
    return _names.services.get(uuid) if _names else None


def appearance(value: int) -> str | None:
    """Exact subcategory name, else the category's (low 6 bits ignored)."""
    if _names is None:
        return None
    return _names.appearance.get(value) or _names.appearance.get(value & ~0x3F)


def parse(sources: dict[str, str]) -> SigNames:
    """`sources` maps SOURCE_FILES keys to the YAML text of each file."""
    import yaml  # only needed when refreshing

    companies = {
        int(item["value"]): str(item["name"])
        for item in yaml.safe_load(sources["companies"])["company_identifiers"]
    }
    services: dict[str, str] = {}
    for key in ("member_services", "services"):  # SIG names win over member names
        for item in yaml.safe_load(sources[key])["uuids"]:
            services[f"0x{int(item['uuid']):04x}"] = str(item["name"])
    appearance: dict[int, str] = {}
    for cat in yaml.safe_load(sources["appearance"])["appearance_values"]:
        base = int(cat["category"]) << 6
        appearance[base] = str(cat["name"])
        for sub in cat.get("subcategory") or []:
            appearance[base | int(sub["value"])] = f"{cat['name']}: {sub['name']}"
    return SigNames(companies, services, appearance, fetched_at=time.time())


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
        return resp.read().decode("utf-8")


def refresh(path: Path, fetch: Callable[[str], str] = _fetch) -> SigNames:
    """Download all four lists, write them to `path` as JSON (atomically, so
    a failed download keeps the previous file) and make them active."""
    loaded = parse({key: fetch(SOURCE_BASE + rel) for key, rel in SOURCE_FILES.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(loaded.to_json(), fh)
    os.replace(tmp, path)
    set_names(loaded)
    return loaded


def load(path: Path) -> SigNames | None:
    """Activate a previously downloaded file; None (built-ins only) when
    there is none or it can't be read."""
    try:
        loaded = SigNames.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        loaded = None
    set_names(loaded)
    return loaded
