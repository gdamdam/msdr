"""BleSupervisor lifecycle against tests/ble/fake_helper.py (never a real
dongle). See docs/ble.md's health-state contract."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from msdr.ble import supervisor as supervisor_module
from msdr.ble.supervisor import BleSupervisor

FAKE_HELPER = Path(__file__).parent / "fake_helper.py"
HELPER_COMMAND = [sys.executable, str(FAKE_HELPER)]


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
        monkeypatch.setenv("FAKE_BLE_MODE", value)

    return _set


async def test_receiving_then_stopped_with_status_shape(mode):
    mode("ok")
    published: list[dict] = []
    sup = BleSupervisor(helper_command=HELPER_COMMAND, on_status=published.append)
    assert sup.health_state() == "stopped"
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == "receiving" and len(sup.table) == 4)
        st = sup.status()
        assert (
            st["controller"]["manufacturer_name"] == "Broadcom"
            and st["controller"]["scan_type"] == "passive"
        )
        assert st["reports"] >= 4 and st["advertiser_count"] == 4 and st["drops"] == 0
        assert st["helper"]["command"].endswith("fake_helper.py")
        assert "no scan responses" in st["coverage"]
        assert sup.table.details("00:11:22:33:44:66")["remote_id"] is True
        await sup.start()  # idempotent while running
        assert sup.running
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped" and not sup.running
    assert any(s["state"] == "receiving" for s in published)


async def test_devices_lists_the_fake_dongle(mode):
    mode("ok")
    sup = BleSupervisor(helper_command=HELPER_COMMAND)
    result = await sup.devices()
    assert result["error"] is None and result["devices"][0]["vid"] == "19ff"
    mode("no_device")
    assert (await sup.devices())["devices"] == []


@pytest.mark.parametrize(
    ("fake_mode", "state"), [("no_device", "no_device"), ("claimed", "claimed"), ("no_le", "no_le")]
)
async def test_terminal_exit_codes_become_states_without_retry(mode, fake_mode, state):
    mode(fake_mode)
    sup = BleSupervisor(helper_command=HELPER_COMMAND)
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == state)
        assert sup.retry_attempt == 0 and sup.last_error
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"


async def test_crash_retries_with_backoff_then_fails(mode, monkeypatch):
    mode("crash")
    monkeypatch.setattr(supervisor_module, "RETRY_BACKOFFS_S", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(supervisor_module, "MAX_RETRIES", 2)
    sup = BleSupervisor(helper_command=HELPER_COMMAND)
    try:
        await sup.start()
        await _wait_for(lambda: sup.health_state() == "failed", timeout=10.0)
        assert sup.retry_attempt == 2 and sup.last_exit_code == 3
        assert sup.last_error == "read failed: simulated"
    finally:
        await sup.stop()


async def test_missing_helper_is_dependency_missing():
    sup = BleSupervisor(
        helper_command=["/nonexistent/python", "-m", "msdr.ble.capture"], usb_id="19ff:0239"
    )
    assert sup.status()["helper"]["command"].endswith("--usb-id 19ff:0239")
    assert sup.status()["evictions"] == 0  # no table yet; counter still present
    try:
        await sup.start()
        assert sup.health_state() == "dependency_missing"
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"
    assert (await sup.devices())["devices"] == []


def test_default_command_uses_this_interpreter():
    assert supervisor_module.default_helper_command()[0] == sys.executable
    assert os.path.basename(FAKE_HELPER) == "fake_helper.py"


async def test_concurrent_starts_spawn_one_helper(monkeypatch):
    spawns = 0
    real_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        await asyncio.sleep(0.02)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    sup = BleSupervisor(helper_command=HELPER_COMMAND)
    try:
        await asyncio.gather(sup.start(), sup.start())
        assert spawns == 1, "the second start must wait for the first, not race its spawn"
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"


async def test_aggregate_survives_ingest_exception_and_counts_it():
    from msdr.ble.observations import AdvertiserTable

    sup = BleSupervisor(helper_command=HELPER_COMMAND)
    sup.table = AdvertiserTable()

    def boom(item):
        raise RuntimeError("bad report")

    sup.table.ingest = boom
    task = asyncio.create_task(sup._aggregate())
    sup._queue.put_nowait({"ts": 1.0})
    sup._queue.put_nowait({"ts": 2.0})
    await _wait_for(lambda: sup.reports >= 2, timeout=2)
    assert not task.done() and sup.table.parse_errors == 2
    task.cancel()
