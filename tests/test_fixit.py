"""The fix-it table: structure, real error strings, and the API."""

import pytest

from msdr import fixit
from msdr.decoder.registry import DECODERS
from tests.api.test_app import running_app

# (error text as the code produces it, expected entry id)
SAMPLES = [
    ("direwolf: required binary 'direwolf' not found", "decoder-missing-direwolf"),
    ("dmr: required binary 'dsd-fme' not found", "decoder-missing-dsd-fme"),
    ("pocsag512: required binary 'multimon-ng' not found", "decoder-missing-multimon-ng"),
    ("rtty_minimodem: required binary 'minimodem' not found", "decoder-missing-minimodem"),
    ("ft8: required binary 'jt9' not found", "decoder-missing-jt9"),
    ("Enable SDRconnect ▸ Preferences ▸ WebSocket Server (port 5454)", "sdrconnect-websocket-off"),
    ("SDRconnect not connected", "sdrconnect-unreachable"),
    ("connection closed", "sdrconnect-unreachable"),
    ("Receiver stopped: press Play in SDRconnect", "receiver-stopped"),
    ("low disk: 3.2 GB free, need 20 GB", "disk-full"),
    ("[Errno 28] No space left on device", "disk-full"),
    ("[Errno 48] error while attempting to bind: address already in use", "port-in-use"),
    ("helper not found", "wifi-helper-missing"),
    (
        "failed to start helper: [Errno 13] Permission denied: '/x/msdr-wifi-capture'",
        "wifi-helper-missing",
    ),
    ("helper's Python venv is broken: /x/python no longer exists", "wifi-helper-missing"),
    ("pyusb or libusb is not available (pip install 'msdr[ble]')", "ble-dependency-missing"),
    ("USB device 0a12:0001 not found", "ble-dongle-access"),
    ("firmware does not answer as Sniffle -- flash it", "ble-dongle-access"),
    ("nextpass not installed", "nextpass-missing"),
]


def test_entries_are_well_formed():
    ids = [f.id for f in fixit.FIXITS]
    assert len(ids) == len(set(ids))
    for f in fixit.FIXITS:
        assert f.patterns and f.title and f.why.strip() and f.steps, f.id
        assert all(s.strip() for s in f.steps), f.id
        assert len(f.actions) <= fixit.MAX_ACTIONS, f.id
        for a in f.actions:
            assert a.kind in ("copy_command", "open_url", "copy_diagnostics")
            assert a.kind == "copy_diagnostics" or a.value, f.id


@pytest.mark.parametrize(("text", "expected"), SAMPLES)
def test_real_error_strings_match_intended_entry(text, expected):
    hit = fixit.match(text)
    assert hit is not None and hit.id == expected


def test_match_is_case_insensitive_and_none_for_unknown():
    assert fixit.match("SDRCONNECT NOT CONNECTED").id == "sdrconnect-unreachable"
    assert fixit.match("tag name already in use") is None
    assert fixit.match("") is None and fixit.match(None) is None


def test_decoder_entries_reuse_registry_install_hint():
    spec = DECODERS["dmr"]
    assert spec.install_hint in fixit.match("required binary 'dsd-fme' not found").steps


async def test_fixit_endpoint():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/fixit", params={"text": "nextpass not installed"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "nextpass-missing" and body["steps"] and len(body["actions"]) <= 3
        assert (await client.get("/api/fixit", params={"text": "zzz"})).status_code == 404


async def test_diagnostics_endpoint_is_plain_text():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/diagnostics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "msdr: " in resp.text and "python: " in resp.text and "sdrconnect: " in resp.text
