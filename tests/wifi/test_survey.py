"""`ChannelSurvey` pure logic against a fake supervisor -- never a real
helper subprocess (that's tests/wifi/test_supervisor.py and
tests/api/test_wifi.py). See docs/history.md "Wi-Fi channel survey"."""

from __future__ import annotations

import asyncio
import time

import pytest

from msdr.wifi.survey import ChannelSurvey

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class FakeTable:
    """Stand-in for NetworkTable.rows() -- entries are plain dicts with the
    fields ChannelSurvey._collect reads (bssid, ssid, channel, last_seen,
    signal_raw_code)."""

    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries = entries or []

    def rows(self, *, sort: str = "first_seen", order: str = "asc", limit: int = 100, **_ignored):
        field = {"signal": "signal_raw_code"}.get(sort, sort)
        candidates = sorted(self.entries, key=lambda e: e["bssid"])
        present = [e for e in candidates if e.get(field) is not None]
        missing = [e for e in candidates if e.get(field) is None]
        present.sort(key=lambda e: e[field], reverse=(order == "desc"))
        candidates = present + missing
        return candidates[:limit], len(candidates)


class FakeSupervisor:
    """A WifiSupervisor stand-in driven by real (short) wall-clock delays:
    `ready_after_s` is how long health_state() reports "starting" before
    flipping to "receiving"; frames tick on a background task so a hop's
    "frames" naturally includes some pre-ready frames that the survey must
    exclude from its delta."""

    def __init__(self, *, ready_after_s: float = 0.0, frame_interval_s: float = 0.01) -> None:
        self.channel: int | None = None
        self.frames = 0
        self.table = FakeTable()
        self.start_calls: list[dict] = []
        self.ready_after_s = ready_after_s
        self.frame_interval_s = frame_interval_s
        self.terminal_state: str | None = None  # set to force a terminal health_state
        self._ready_at: float | None = None
        self._frame_task: asyncio.Task | None = None

    async def start(
        self, channel: int, *, configured_channel=None, keep_table: bool = False
    ) -> None:
        self.start_calls.append(
            {"channel": channel, "configured_channel": configured_channel, "keep_table": keep_table}
        )
        self.channel = channel
        self.frames = 0
        self._ready_at = time.monotonic() + self.ready_after_s
        if self._frame_task is not None:
            self._frame_task.cancel()
        self._frame_task = asyncio.create_task(self._tick_frames())

    async def _tick_frames(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.frame_interval_s)
                self.frames += 1
        except asyncio.CancelledError:
            raise

    def health_state(self) -> str:
        if self.terminal_state is not None:
            return self.terminal_state
        assert self._ready_at is not None
        return "receiving" if time.monotonic() >= self._ready_at else "starting"


async def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def test_hop_order_and_keep_table_true():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[1, 6, 11], dwell_s=0.03, rounds=1)
    survey.start()
    await survey.task
    assert [c["channel"] for c in sup.start_calls] == [1, 6, 11]
    assert all(c["keep_table"] is True for c in sup.start_calls)
    assert all(c["configured_channel"] == c["channel"] for c in sup.start_calls)
    assert survey.state == "done"
    assert survey.ended_at is not None


async def test_rounds_repeat_the_channel_list():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[1, 6], dwell_s=0.02, rounds=2)
    survey.start()
    await survey.task
    assert [c["channel"] for c in sup.start_calls] == [1, 6, 1, 6]
    assert survey.round == 2
    for ch in (1, 6):
        result = next(r for r in survey.status()["results"] if r["channel"] == ch)
        assert result["visits"] == 2


async def test_listened_s_excludes_startup_time():
    sup = FakeSupervisor(ready_after_s=0.15, frame_interval_s=0.01)
    survey = ChannelSurvey(sup, channels=[6], dwell_s=0.1, rounds=1)
    survey.start()
    await survey.task
    result = survey.status()["results"][0]
    # listened_s should track the dwell (~0.1s), not the startup wait too.
    assert 0.06 <= result["listened_s"] <= 0.25


async def test_frames_delta_excludes_frames_received_before_ready():
    sup = FakeSupervisor(ready_after_s=0.1, frame_interval_s=0.01)
    survey = ChannelSurvey(sup, channels=[6], dwell_s=0.1, rounds=1)
    survey.start()
    await survey.task
    result = survey.status()["results"][0]
    # Frames ticked for the whole ~0.2s hop (startup + dwell), but the
    # reported delta must only cover the ~0.1s dwell.
    assert result["frames"] < sup.frames
    assert result["frames"] > 0


async def test_networks_counted_only_inside_the_listened_window():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[6], dwell_s=0.05, rounds=1)
    survey.start()

    async def _populate():
        await _wait_for(lambda: survey.current_channel == 6)
        t0 = time.time()
        sup.table.entries = [
            {
                "bssid": "aa:bb:cc:dd:ee:01",
                "ssid": "inside",
                "channel": 6,
                "last_seen": t0 + 0.01,
                "signal_raw_code": 50,
            },
            {
                "bssid": "aa:bb:cc:dd:ee:02",
                "ssid": "before",
                "channel": 6,
                "last_seen": t0 - 5.0,
                "signal_raw_code": 10,
            },
            {
                "bssid": "aa:bb:cc:dd:ee:03",
                "ssid": "after",
                "channel": 6,
                "last_seen": t0 + 1000.0,
                "signal_raw_code": 90,
            },
        ]

    await _populate()
    await survey.task
    result = survey.status()["results"][0]
    assert result["networks"] == 1
    assert [n["bssid"] for n in result["top"]] == ["aa:bb:cc:dd:ee:01"]


async def test_ds_channel_reported_from_row_channel_mismatch():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[6], dwell_s=0.05, rounds=1)
    survey.start()

    async def _populate():
        await _wait_for(lambda: survey.current_channel == 6)
        t0 = time.time()
        sup.table.entries = [
            {
                "bssid": "aa:bb:cc:dd:ee:01",
                "ssid": "overlap",
                "channel": 11,
                "last_seen": t0 + 0.01,
                "signal_raw_code": 50,
            },
        ]

    await _populate()
    await survey.task
    result = survey.status()["results"][0]
    assert result["top"][0]["ds_channel"] == 11


async def test_top_sorted_by_signal_strongest_first():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[6], dwell_s=0.05, rounds=1)
    survey.start()

    async def _populate():
        await _wait_for(lambda: survey.current_channel == 6)
        t0 = time.time()
        sup.table.entries = [
            {
                "bssid": "aa:bb:cc:dd:ee:01",
                "ssid": "weak",
                "channel": 6,
                "last_seen": t0 + 0.01,
                "signal_raw_code": 10,
            },
            {
                "bssid": "aa:bb:cc:dd:ee:02",
                "ssid": "strong",
                "channel": 6,
                "last_seen": t0 + 0.01,
                "signal_raw_code": 90,
            },
        ]

    await _populate()
    await survey.task
    top = survey.status()["results"][0]["top"]
    assert [n["bssid"] for n in top] == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"]


async def test_stop_mid_dwell():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[1, 6, 11], dwell_s=5.0, rounds=1)
    survey.start()
    await _wait_for(lambda: survey.current_channel == 1)
    await asyncio.sleep(0.05)
    await survey.stop()
    assert survey.state == "stopped"
    assert survey.ended_at is not None
    # Stopped well before the 5s dwell elapsed, and never reached channel 6.
    result = next(r for r in survey.status()["results"] if r["channel"] == 1)
    assert result["listened_s"] < 1.0
    assert all(c["channel"] != 6 for c in sup.start_calls)


async def test_stop_before_any_hop_completes_is_idempotent():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[1], dwell_s=5.0, rounds=1)
    survey.start()
    await _wait_for(lambda: survey.current_channel == 1)
    await survey.stop()
    await survey.stop()  # idempotent -- already stopped, must not raise
    assert survey.state == "stopped"


async def test_terminal_health_state_fails_the_survey():
    sup = FakeSupervisor()
    sup.terminal_state = "no_device"
    survey = ChannelSurvey(sup, channels=[1, 6], dwell_s=0.05, rounds=1)
    survey.start()
    await survey.task
    assert survey.state == "failed"
    assert survey.error is not None
    assert "no_device" in survey.error
    # A failed hop is skipped, not fatal: both channels were tried, both
    # failed, and with no channel heard at all the survey ends "failed".
    assert [c["channel"] for c in sup.start_calls] == [1, 6]
    assert [r["status"] for r in survey.status()["results"]] == ["failed", "failed"]


async def test_rounds_zero_runs_until_stopped():
    sup = FakeSupervisor()
    survey = ChannelSurvey(sup, channels=[1, 6], dwell_s=0.02, rounds=0)
    survey.start()
    await _wait_for(lambda: survey.round >= 2)
    await survey.stop()
    assert survey.state == "stopped"


async def test_phase_and_status_track_switching_then_listening():
    sup = FakeSupervisor(ready_after_s=0.2)
    survey = ChannelSurvey(sup, channels=[1, 6], dwell_s=0.3, rounds=1)
    survey.start()
    await _wait_for(lambda: survey.phase == "switching")
    status = survey.status()
    # No stale countdown while the helper restarts.
    assert status["channel_ends_at"] is None
    assert [r["status"] for r in status["results"]] == ["switching", "pending"]
    await _wait_for(lambda: survey.phase == "listening")
    assert survey.status()["channel_ends_at"] is not None
    assert survey.status()["results"][0]["status"] == "listening"
    await survey.task
    status = survey.status()
    assert status["phase"] is None
    assert [r["status"] for r in status["results"]] == ["done", "done"]
    assert status["switch_s"] >= 0.15


async def test_listening_channel_reports_live_results():
    sup = FakeSupervisor(frame_interval_s=0.01)
    survey = ChannelSurvey(sup, channels=[6], dwell_s=1.0, rounds=1)
    survey.start()
    await _wait_for(lambda: survey.phase == "listening")
    sup.table.entries.append(
        {"bssid": "aa:bb:cc:dd:ee:ff", "ssid": "Home", "channel": 6,
         "last_seen": time.time(), "signal_raw_code": -40}
    )
    await asyncio.sleep(0.1)
    row = survey.status()["results"][0]
    assert row["visits"] == 0  # the dwell isn't over yet...
    assert row["frames"] > 0 and row["listened_s"] > 0  # ...but it is counted live
    assert row["networks"] == 1 and row["top"][0]["ssid"] == "Home"
    await survey.stop()


async def test_request_stop_keeps_partial_dwell_and_estimate():
    sup = FakeSupervisor(frame_interval_s=0.01)
    survey = ChannelSurvey(sup, channels=[1, 6, 11], dwell_s=5.0, rounds=2)
    assert survey.status()["estimated_total_s"] == 3 * 2 * (5.0 + 5.0)  # guessed restart
    survey.start()
    await _wait_for(lambda: survey.phase == "listening")
    await asyncio.sleep(0.1)
    survey.request_stop()
    assert survey.state == "stopping"
    first = survey.status()["results"][0]
    assert first["status"] == "done" and first["visits"] == 1 and first["frames"] > 0
    await survey.stop(finalize=False)
    assert survey.state == "stopping"  # caller still restoring the prior capture
    await survey.stop()
    assert survey.state == "stopped"


async def test_unexpected_error_fails_the_survey_instead_of_hanging():
    class BrokenTable(FakeTable):
        def rows(self, **_kw):
            raise TypeError("boom")

    sup = FakeSupervisor()
    sup.table = BrokenTable()
    survey = ChannelSurvey(sup, channels=[1, 6], dwell_s=0.05, rounds=1)
    survey.start()
    await survey.task  # ends; doesn't leave a dead task behind a "running" state
    status = survey.status()
    assert status["state"] == "failed" and "internal error: TypeError: boom" in status["error"]
    assert status["phase"] is None
    assert [r["status"] for r in status["results"]] == ["pending", "pending"]
    await survey.stop()  # and Stop no longer re-raises


class FlakySupervisor(FakeSupervisor):
    """health_state() is terminal on the channels in `bad`; others start fine."""

    def __init__(self, bad: set[int], **kw) -> None:
        super().__init__(**kw)
        self.bad = bad
        self.last_error = "firmware checksum report timed out; REG_MCUFWDL=0x00060000"

    def health_state(self) -> str:
        return "failed" if self.channel in self.bad else super().health_state()


async def test_failed_channel_is_recorded_and_skipped_with_helper_error():
    sup = FlakySupervisor(bad={6})
    survey = ChannelSurvey(sup, channels=[1, 6, 11], dwell_s=0.03, rounds=1)
    survey.start()
    await survey.task
    status = survey.status()
    assert status["state"] == "done" and status["failed_channels"] == 1
    by = {r["channel"]: r for r in status["results"]}
    assert by[1]["status"] == "done" and by[11]["status"] == "done"
    assert by[6]["status"] == "failed" and by[6]["visits"] == 0
    assert "firmware checksum" in by[6]["error"]  # the helper's own error, not just "timed out"


async def test_three_consecutive_failures_abort_the_survey():
    sup = FlakySupervisor(bad={1, 6, 11})
    survey = ChannelSurvey(sup, channels=[1, 6, 11, 36, 40], dwell_s=0.03, rounds=1)
    survey.start()
    await survey.task
    status = survey.status()
    assert status["state"] == "failed"
    assert status["error"].startswith("3 channels in a row could not be started")
    assert [r["status"] for r in status["results"]] == ["failed"] * 3 + ["pending"] * 2
    assert [c["channel"] for c in sup.start_calls] == [1, 6, 11]  # 36/40 never attempted
