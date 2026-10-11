"""WifiSupervisor lifecycle, against tests/wifi/fake_helper.py (never a real
adapter). See docs/wifi.md's health-state contract."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import sys
import time
import types
from pathlib import Path

import pytest

from msdr.wifi import supervisor as supervisor_module
from msdr.wifi.pcap import Record
from msdr.wifi.supervisor import WifiSupervisor

FAKE_HELPER = Path(__file__).parent / "fake_helper.py"
HELPER_COMMAND = [sys.executable, str(FAKE_HELPER)]


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def test_lifecycle_unavailable_then_receiving_then_stopped(tmp_path):
    sup = WifiSupervisor(helper_path=str(tmp_path / "does-not-exist"))
    assert sup.health_state() == "stopped"
    await sup.start(6)
    assert sup.health_state() == "unavailable"
    await sup.stop()
    assert sup.health_state() == "stopped"

    sup2 = WifiSupervisor(helper_command=HELPER_COMMAND)
    await sup2.start(6)
    await _wait_for(lambda: sup2.health_state() == "receiving")
    assert sup2.frames > 0
    assert sup2.channel == 6
    assert sup2.ready is not None
    assert sup2.ready.upstream_commit == "347e9f172750e0c0b7c45b6dcfef7e780d70d144"
    status = sup2.status()
    assert status["helper"]["path"] == " ".join(HELPER_COMMAND)
    assert status["helper"]["host_tx_dma_disabled"] is True
    assert status["helper"]["mac_tx_enabled"] is False
    await sup2.stop()
    assert sup2.health_state() == "stopped"


async def test_no_device_state(monkeypatch):
    monkeypatch.setenv("MSDR_WIFI_FAKE_MODE", "no_device")
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    await sup.start(6)
    await _wait_for(lambda: sup.health_state() == "no_device")
    assert sup.last_exit_code == 1
    await sup.stop()
    assert sup.health_state() == "stopped"


async def test_stale_after_no_frames_for_a_while():
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    await sup.start(6)
    await _wait_for(lambda: sup.health_state() == "receiving")
    sup.last_frame_at = time.time() - (supervisor_module.STALE_AFTER_S + 1)
    assert sup.health_state() == "stale"
    await sup.stop()


async def test_crash_then_respawns_and_resumes_receiving(monkeypatch):
    monkeypatch.setattr(supervisor_module, "RETRY_BACKOFFS_S", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setenv("MSDR_WIFI_FAKE_MODE", "crash")
    monkeypatch.setenv("MSDR_WIFI_FAKE_CRASH_AFTER", "3")
    monkeypatch.setenv("MSDR_WIFI_FAKE_DELAY_S", "0.01")
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    await sup.start(6)
    await _wait_for(lambda: sup.retry_attempt >= 1 or sup.last_exit_code == 3, timeout=3)
    assert sup.last_exit_code == 3
    # The respawned process reaches "ready" again, resetting retry_attempt.
    await _wait_for(lambda: sup.health_state() == "receiving" and sup.retry_attempt == 0, timeout=3)
    await sup.stop()
    assert sup.health_state() == "stopped"


async def test_retry_scheduled_while_under_budget(monkeypatch):
    monkeypatch.setattr(supervisor_module, "RETRY_BACKOFFS_S", (5.0,))
    sup = WifiSupervisor(helper_command=["/bin/true"])
    sup._wanted = True
    sup.channel = 6
    sup._process = types.SimpleNamespace(returncode=3)
    await sup._on_process_exited()
    try:
        assert sup.retry_attempt == 1
        assert sup.retry_at is not None
        assert sup._retry_task is not None
        # A scheduled retry means the receiver is coming back (same contract
        # as BleSupervisor); "failed" is reserved for an exhausted budget.
        assert sup.health_state() == "starting"
        sup.retry_attempt = supervisor_module.MAX_RETRIES
        assert sup.health_state() == "failed"
    finally:
        sup._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sup._retry_task


async def test_retries_exhausted_reports_failed_with_no_further_retry(monkeypatch):
    monkeypatch.setattr(supervisor_module, "MAX_RETRIES", 2)
    sup = WifiSupervisor(helper_command=["/bin/true"])
    sup._wanted = True
    sup.channel = 6
    sup.retry_attempt = 2  # already at the cap
    sup._process = types.SimpleNamespace(returncode=3)
    await sup._on_process_exited()
    assert sup._retry_task is None
    assert sup.health_state() == "failed"
    await sup.stop()
    assert sup.health_state() == "stopped"


async def test_stop_cleanup_no_zombie_and_tasks_cancelled():
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    await sup.start(6)
    await _wait_for(lambda: sup.health_state() == "receiving")
    pid = sup._process.pid
    await sup.stop()
    assert sup._process is None
    assert sup._stdout_task is None
    assert sup._stderr_task is None
    assert sup._aggregator_task is None
    assert sup._tick_task is None
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, 0)
    # Idempotent: a second stop() on an already-stopped supervisor is a no-op.
    await sup.stop()


def test_enqueue_drops_and_counts_when_queue_full():
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    sup._queue = asyncio.Queue(maxsize=3)
    record = Record(ts=0.0, linktype=127, data=b"", orig_len=0)
    for _ in range(3):
        sup._enqueue(record)
    assert sup.drops == 0
    assert sup._queue.qsize() == 3
    for _ in range(5):
        sup._enqueue(record)
    assert sup.drops == 5
    assert sup._queue.qsize() == 3


async def test_malformed_frames_counted_without_crashing(monkeypatch):
    monkeypatch.setenv("MSDR_WIFI_FAKE_MODE", "malformed")
    monkeypatch.setenv("MSDR_WIFI_FAKE_DELAY_S", "0.005")
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    await sup.start(6)
    await _wait_for(lambda: sup.frames >= 25, timeout=5)
    assert sup.parse_errors > 0
    assert sup.health_state() in ("receiving", "stale")
    await sup.stop()


async def test_devices_lists_fake_helper_output():
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    result = await sup.devices()
    assert result["error"] is None
    assert result["devices"] == [
        {
            "vid": 3034,
            "pid": 34834,
            "bus": 1,
            "address": 5,
            "product": "802.11n NIC",
            "serial": "123456",
            "supported": True,
        }
    ]


async def test_devices_empty_when_no_adapter(monkeypatch):
    monkeypatch.setenv("MSDR_WIFI_FAKE_LIST_EMPTY", "1")
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    result = await sup.devices()
    assert result["devices"] == []


async def test_devices_helper_missing():
    sup = WifiSupervisor(helper_path="/no/such/helper")
    result = await sup.devices()
    assert result["devices"] == []
    assert result["error"]


def test_supervisor_module_never_imports_sdr_or_recorder_packages():
    # Independence (docs/wifi.md): the supervisor never touches SDRconnect,
    # receiver ownership, the pre-roll recorder, scanner, survey or tracking.
    import ast

    tree = ast.parse(Path(supervisor_module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_roots = ("msdr.radio", "msdr.api", "msdr.recorder", "msdr.engine", "msdr.survey")
    assert not any(name.startswith(root) for name in imported for root in forbidden_roots)


async def test_concurrent_starts_spawn_one_helper(monkeypatch):
    spawns = 0
    real_exec = asyncio.create_subprocess_exec

    async def counting_exec(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        await asyncio.sleep(0.02)  # widen the window the lock has to cover
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_exec)
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    try:
        await asyncio.gather(sup.start(6), sup.start(6))
        assert spawns == 1, "the second start must wait for the first, not race its spawn"
        await _wait_for(lambda: sup.health_state() == "receiving")
    finally:
        await sup.stop()
    assert sup.health_state() == "stopped"


def _script(path: Path, interpreter: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{interpreter}\nprint('hi')\n")
    path.chmod(0o755)
    return path


def test_find_helper_prefers_setting_then_path_then_sibling(tmp_path, monkeypatch):
    sibling = _script(tmp_path / "sib/.venv/bin/msdr-wifi-capture", sys.executable)
    monkeypatch.setattr(supervisor_module, "SIBLING_HELPER_PATH", sibling)
    assert supervisor_module.find_helper(None) == sibling
    # A configured path that doesn't exist falls through to the search.
    assert supervisor_module.find_helper(str(tmp_path / "missing")) == sibling

    on_path = _script(tmp_path / "bin/msdr-wifi-capture-x", sys.executable)
    monkeypatch.setattr(supervisor_module, "HELPER_NAME", on_path.name)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert supervisor_module.find_helper(None) == on_path

    configured = _script(tmp_path / "mine/msdr-wifi-capture", sys.executable)
    assert supervisor_module.find_helper(str(configured)) == configured


async def test_moved_venv_is_reported_with_rebuild_command(tmp_path):
    helper = _script(
        tmp_path / "helper/.venv/bin/msdr-wifi-capture", "/old/place/.venv/bin/python3"
    )
    assert supervisor_module.helper_problem(_script(tmp_path / "ok", sys.executable)) is None
    problem = supervisor_module.helper_problem(helper)
    assert "/old/place/.venv/bin/python3 no longer exists" in problem
    assert f"cd {tmp_path / 'helper'} && rm -rf .venv" in problem

    sup = WifiSupervisor(helper_path=str(helper))
    assert (await sup.devices())["error"] == problem
    await sup.start(6)
    assert sup.health_state() == "dependency_missing"
    assert sup.last_error == problem
    await sup.stop()


async def test_aggregate_survives_ingest_exception_and_counts_it():
    from msdr.wifi.observations import NetworkTable

    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    sup.table = NetworkTable(configured_channel=6)

    def boom(record, rt):
        raise RuntimeError("bad frame")

    sup.table.ingest = boom
    task = asyncio.create_task(sup._aggregate())
    for ts in (1.0, 2.0):
        data = b"\x00\x00\x08\x00\x00\x00\x00\x00"
        sup._queue.put_nowait(Record(ts=ts, linktype=127, data=data, orig_len=8))
    await _wait_for(lambda: sup.frames >= 2, timeout=2)
    assert not task.done() and sup.parse_errors == 2
    task.cancel()


async def test_start_cancelled_part_way_leaves_no_orphan_helper():
    """A survey hop stopped while its helper is starting cancels the caller
    of start(); the stop-and-spawn body must still complete, so the helper
    is registered (and reaped by the next stop()) rather than orphaned."""
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    task = asyncio.create_task(sup.start(6))
    await asyncio.sleep(0)  # into start(); its body is now in flight
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await _wait_for(lambda: sup._process is not None)
    proc = sup._process
    await sup.stop()
    assert proc.returncode is not None  # reaped, not left running
    gc.collect()  # a leaked process/pipe would surface as a ResourceWarning here


class _StuckProc:
    """A helper that ignores SIGTERM (so stop() is still waiting when it is
    cancelled) and exits only on kill()."""

    pid = 4242

    def __init__(self) -> None:
        self.returncode = None
        self._exit = asyncio.Event()

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        self.returncode = -9
        self._exit.set()

    async def wait(self) -> int:
        await self._exit.wait()
        return self.returncode


async def test_stop_cancelled_mid_termination_still_reaps_the_helper():
    """App shutdown cancels a survey's background stop while it waits for
    the helper to exit; the helper must not be left running past the loop."""
    sup = WifiSupervisor(helper_command=HELPER_COMMAND)
    proc = _StuckProc()
    sup._process = proc
    sup._wanted = True
    stopping = asyncio.create_task(sup.stop())
    await _wait_for(lambda: sup._process is None)  # inside _terminate_process's wait
    stopping.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stopping
    assert sup._zombies == [proc] and proc.returncode is not None  # killed, not reaped yet
    await sup.stop()  # reaps it
    assert sup._zombies == []
