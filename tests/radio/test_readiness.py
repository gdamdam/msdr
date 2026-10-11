"""Unit coverage for msdr.radio.readiness: SDRconnect's per-host gate.

Uses a fake host:port per test (not a real socket) and monkeypatches
_probe_once so these run instantly, independent of tests/radio/test_sdrconnect.py's
end-to-end coverage against FakeSDRconnect.
"""

import itertools

import pytest

from msdr.radio import readiness

_ports = itertools.count(40000)


@pytest.fixture
def hostport():
    # A fresh host:port key per test keeps readiness._state isolated.
    return "127.0.0.1", next(_ports)


async def test_wait_ready_warns_and_proceeds_without_a_positive_rate(hostport, monkeypatch, caplog):
    host, port = hostport
    monkeypatch.setattr(readiness, "_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(readiness, "_PROBE_INTERVAL_S", 0.01)

    async def never_ready(ws, timeout_s):
        return False

    monkeypatch.setattr(readiness, "_probe_once", never_ready)

    with caplog.at_level("WARNING"):
        settle_wait_s = await readiness.wait_ready(object(), host, port, startup_grace_s=1.0)

    assert settle_wait_s == 0.0
    assert "continuing without grace" in caplog.text


async def test_settle_applies_after_mark_down_not_on_first_attach(hostport, monkeypatch):
    host, port = hostport
    monkeypatch.setattr(readiness, "_MIN_SETTLE_S", 5.0)

    async def always_ready(ws, timeout_s):
        return True

    monkeypatch.setattr(readiness, "_probe_once", always_ready)

    first_wait = await readiness.wait_ready(object(), host, port, startup_grace_s=1.0)
    assert first_wait == 0.0  # very first attachment: no forced settle

    readiness.mark_down(host, port)
    relaunch_wait = await readiness.wait_ready(object(), host, port, startup_grace_s=1.0)
    assert relaunch_wait > 0.0  # server-side drop: settle applies
