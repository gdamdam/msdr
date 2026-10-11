"""IEEE MAC vendor names (msdr/oui.py): parse, download-to-file, load, most
specific prefix wins, locally administered addresses never get a vendor.
Tiny hand-written CSV in the registry's shape -- no network, no IEEE data."""

import pytest

from msdr import oui

HEADER = "Registry,Assignment,Organization Name,Organization Address\n"
FILES = {
    "MA-L": HEADER + 'MA-L,D03F27,"Wyze Labs Inc","Kirkland WA US"\n'
    + 'MA-L,70B3D5,IEEE Registration Authority,"Piscataway NJ US"\n',
    "MA-M": HEADER + "MA-M,C85CE27,SYNERGY SYSTEMS,Faridabad IN\n",
    "MA-S": HEADER + 'MA-S,70B3D5ABC,"Small Maker, Inc.",Somewhere\n',
}


@pytest.fixture(autouse=True)
def _reset():
    oui.set_names(None)
    yield
    oui.set_names(None)


def _fetch(url: str) -> str:
    return FILES[next(k for k, u in oui.SOURCES.items() if u == url)]


def test_refresh_and_lookups(tmp_path):
    path = tmp_path / "wifi" / "oui.json"
    assert oui.vendor("d0:3f:27:a5:ad:da") is None  # nothing downloaded yet
    assert oui.refresh(path, fetch=_fetch).summary()["prefixes"] == 4
    assert oui.vendor("d0:3f:27:a5:ad:da") == "Wyze Labs Inc"
    assert oui.vendor("C8-5C-E2-71-00-01") == "SYNERGY SYSTEMS"  # MA-M, any separator
    # The MA-S block beats the IEEE's own MA-L block it sits in.
    assert oui.vendor("70:b3:d5:ab:c1:23") == "Small Maker, Inc."
    assert oui.vendor("70:b3:d5:00:00:01") == "IEEE Registration Authority"
    assert oui.vendor("00:00:00:00:00:00") is None
    oui.set_names(None)
    assert oui.load(path).summary()["prefixes"] == 4


def test_locally_administered_never_has_a_vendor(tmp_path):
    oui.refresh(tmp_path / "oui.json", fetch=_fetch)
    # d2 = d0 with the 0x02 bit: a router's derived guest/mesh BSSID.
    assert oui.is_locally_administered("d2:3f:27:a5:ad:da")
    assert not oui.is_locally_administered("d0:3f:27:a5:ad:da")
    assert oui.vendor("d2:3f:27:a5:ad:da") is None


def test_failed_download_keeps_previous_file(tmp_path):
    path = tmp_path / "oui.json"
    oui.refresh(path, fetch=_fetch)
    before = path.read_text()

    def broken(url: str) -> str:
        raise OSError("offline")

    with pytest.raises(OSError):
        oui.refresh(path, fetch=broken)
    assert path.read_text() == before
