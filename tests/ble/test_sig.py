"""Bluetooth SIG names (msdr/ble/sig.py): parse, download-to-file, load and
the lookups' fallback from the built-in tables. Tiny hand-written YAML in the
SIG's file shape -- no network and no SIG data in the repo."""

import pytest

from msdr.ble import adv, sig

FILES = {
    "companies": "company_identifiers:\n  - value: 0x004C\n    name: 'Apple, Inc.'\n"
    "  - value: 0x1130\n    name: 'Pi Variables, Inc.'\n",
    "services": "uuids:\n  - uuid: 0x180F\n    name: 'Battery'\n"
    "    id: org.bluetooth.service.battery\n",
    "member_services": "uuids:\n  - uuid: 0xFEED\n    name: 'Tile, Inc.'\n"
    "  - uuid: 0x180F\n    name: 'member name loses'\n",
    "appearance": "appearance_values:\n  - category: 0x025\n    name: 'Wearable Audio Device'\n"
    "    subcategory:\n      - value: 0x01\n        name: 'Earbud'\n",
}


@pytest.fixture(autouse=True)
def _reset():
    sig.set_names(None)
    yield
    sig.set_names(None)


def _fetch(url: str) -> str:
    key = next(k for k, rel in sig.SOURCE_FILES.items() if url == sig.SOURCE_BASE + rel)
    return FILES[key]


def test_refresh_writes_file_and_lookups_fall_back_to_it(tmp_path):
    path = tmp_path / "ble" / "sig.json"
    assert adv.company_name(0x1130) is None  # built-ins only before a download
    names = sig.refresh(path, fetch=_fetch)
    assert path.exists() and names.summary()["companies"] == 2
    assert adv.company_name(0x1130) == "Pi Variables, Inc."
    assert adv.company_name(0x004C) == "Apple"  # the short built-in name still wins
    assert adv.service_name("0xfeed") == "Tile, Inc."
    assert sig.service("0x180f") == "Battery"  # SIG name beats the member entry
    earbud = (0x025 << 6) | 0x01
    assert adv.appearance_name(earbud) == "Wearable Audio Device: Earbud"
    assert adv.appearance_name(0x025 << 6 | 0x05) == "Wearable Audio Device"  # unknown sub
    sig.set_names(None)
    assert sig.load(path).summary()["services"] == 2
    assert adv.company_name(0x1130) == "Pi Variables, Inc."


def test_failed_download_keeps_previous_file(tmp_path):
    path = tmp_path / "sig.json"
    sig.refresh(path, fetch=_fetch)
    before = path.read_text()

    def broken(url: str) -> str:
        raise OSError("offline")

    with pytest.raises(OSError):
        sig.refresh(path, fetch=broken)
    assert path.read_text() == before
    assert sig.load(tmp_path / "missing.json") is None
