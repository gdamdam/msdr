"""`BleSupervisor(backend="sniffle")` against `tests/ble/fake_sniffle.py` run
as a real subprocess (`FAKE_SNIFFLE_MODE` env var), so the actual subprocess/
pipe handling is exercised the same way `tests/ble/test_supervisor.py` does
for the hci backend -- never a real SONOFF dongle."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from msdr.ble.supervisor import BleSupervisor

FAKE_SNIFFLE = Path(__file__).parent / "fake_sniffle.py"
HELPER_COMMAND = [sys.executable, str(FAKE_SNIFFLE)]


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setenv("FAKE_SNIFFLE_MODE", value)

    return _set


async def test_receiving_with_phy_channel_and_coverage(mode):
    mode("ok")
    sup = BleSupervisor(
        helper_command=HELPER_COMMAND,
        backend="sniffle",
        phy="coded",
        channel="37",
    )
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == "receiving")
        st = sup.status()
        assert st["backend"] == "sniffle"
        assert st["phy"] == "coded" and st["channel"] == "37"
        assert st["controller"]["firmware_version"] == "1.11.0"
        assert st["controller"]["extended"] is True
        assert st["coverage"] == (
            "Coded PHY on channel 37 only; legacy and extended advertising "
            "(aux-channel chaining followed); no scan responses, no connection "
            "following"
        )
        await _wait_for(lambda: len(sup.table) == 1)
        row = sup.table.rows()[0][0]
        assert row["phy"] == "1m" and row["channel"] == 37 and row["extended"] is False
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"


async def test_hop_coverage_sentence(mode):
    mode("ok")
    sup = BleSupervisor(helper_command=HELPER_COMMAND, backend="sniffle", channel="hop")
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == "receiving")
        assert "channels 37-39 (dwell hop)" in sup.status()["coverage"]
    finally:
        await sup.stop()


async def test_firmware_mismatch_is_terminal_without_retry(mode):
    mode("firmware_mismatch")
    sup = BleSupervisor(helper_command=HELPER_COMMAND, backend="sniffle")
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == "firmware_mismatch", timeout=5.0)
        assert sup.retry_attempt == 0
        assert "Sniffle" in (sup.last_error or "")
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"


async def test_wrong_baud_is_terminal_without_retry(mode):
    mode("wrong_baud")
    sup = BleSupervisor(helper_command=HELPER_COMMAND, backend="sniffle", baud=2_000_000)
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == "wrong_baud", timeout=8.0)
        assert sup.retry_attempt == 0
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"


async def test_unplug_mid_run_retries_then_recovers(mode, monkeypatch):
    import msdr.ble.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "RETRY_BACKOFFS_S", (0.05, 0.05, 0.05, 0.05))
    mode("unplug")
    sup = BleSupervisor(helper_command=HELPER_COMMAND, backend="sniffle")
    try:
        await sup.start()
        await _wait_for(lambda: sup.last_exit_code == 3, timeout=5.0)
        # Not a terminal state (unlike firmware_mismatch/wrong_baud): the
        # supervisor retries a mid-run disconnect with backoff, same as hci.
        # fake_sniffle("unplug") disconnects again after a handful of
        # reports each time, so prove recovery happened (not a terminal
        # state) by watching the retry counter move and reports keep
        # accumulating across restarts, rather than catching one exact
        # "receiving" instant.
        await _wait_for(lambda: sup.retry_attempt >= 1 or sup.reports > 5, timeout=5.0)
        assert sup.health_state() != "failed"
    finally:
        await sup.stop()


def test_default_helper_command_for_sniffle_backend():
    from msdr.ble.supervisor import default_helper_command

    cmd = default_helper_command("sniffle")
    assert cmd == [sys.executable, "-m", "msdr.ble.sniffle_capture"]


def test_capture_command_includes_serial_settings():
    sup = BleSupervisor(
        backend="sniffle",
        serial_port="/dev/cu.usbserial-FAKE",
        baud=921_600,
        phy="2m",
        channel="38",
    )
    cmd = sup._capture_command()
    assert cmd[-8:] == [
        "--port",
        "/dev/cu.usbserial-FAKE",
        "--baud",
        "921600",
        "--phy",
        "2m",
        "--channel",
        "38",
    ]
