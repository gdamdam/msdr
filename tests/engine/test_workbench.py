import asyncio
import time
from types import SimpleNamespace

import pytest

from msdr.engine.workbench import SignalWorkbench, WatchConfig
from msdr.radio.protocol import SpectrumFrame


class FakeHub:
    def __init__(self):
        self.queue = None
        self.released = 0

    def acquire(self):
        self.queue = asyncio.Queue()
        return self.queue

    def release(self, queue):
        assert queue is self.queue
        self.released += 1
        self.queue = None


class FakeRecorder:
    def __init__(self):
        self.state = "off"
        self.stops = 0

    def status(self):
        return {"state": self.state}


class FakeService:
    def __init__(self):
        self.adapter = SimpleNamespace(connected=True)
        self.confirmed = SimpleNamespace(
            props={"started": True, "device_center_frequency": 145_800_000,
                   "device_sample_rate": 2_000_000, "device_vfo_frequency": 145_800_000},
            is_complete=lambda: True,
        )
        self.spectrum_hub = FakeHub()
        self.scanner = SimpleNamespace(state="idle")
        self._band_scan_active = False
        self.doppler_loop = SimpleNamespace(status=None)
        self.beacons = None
        self.decoder = None
        self.recorder = FakeRecorder()
        self.settings = SimpleNamespace(host="127.0.0.1", port=5454)
        self.tunes = []
        self.record_starts = 0
        self._rec_obs = None
        self.observations = SimpleNamespace(get=lambda _id: None, update=lambda _obs: None)
        self.workbench = None
        self.owner_publications = 0

    async def _band_scan_tune_span(self, rate, center, *, kind):
        assert kind == "watch_span"
        self.tunes.append((rate, center))
        self.confirmed.props["device_sample_rate"] = rate
        self.confirmed.props["device_center_frequency"] = center
        if self.workbench is not None:
            await self.workbench.receiver_changed()
        return True

    def _require_recorder_feature(self):
        pass

    async def recorder_arm(self, on):
        self.recorder.state = "armed" if on else "off"
        return self.recorder.status()

    async def recorder_start(self):
        self.record_starts += 1
        self.recorder.state = "recording"
        return self.recorder.status()

    async def recorder_stop(self):
        self.recorder.stops += 1
        self.recorder.state = "finalizing"
        return self.recorder.status()

    async def _publish_owner(self):
        self.owner_publications += 1


def _workbench():
    service = FakeService()
    workbench = SignalWorkbench(service)
    service.workbench = workbench
    return service, workbench


@pytest.mark.asyncio
async def test_confirmed_group_tunes_keep_watch_alive_and_release_spectrum():
    service, workbench = _workbench()
    config = WatchConfig(frequencies_hz=[100_000_000, 110_000_000], cycle_groups=True)
    await workbench.start_watch(config)
    for _ in range(20):
        if service.tunes:
            break
        await asyncio.sleep(0.01)
    assert service.tunes
    assert workbench.active
    assert workbench.group_index == 0
    assert service.confirmed.props["device_center_frequency"] == service.tunes[0][1]
    assert workbench._last_frame_at is None
    await workbench.stop_watch()
    assert not workbench.active
    assert service.spectrum_hub.released == 1
    assert service.owner_publications >= 1


@pytest.mark.asyncio
async def test_manual_span_change_flushes_queued_frames_and_stale_coverage():
    service, workbench = _workbench()
    frequency = 145_800_000 + 300_000
    await workbench.start_watch(WatchConfig(frequencies_hz=[frequency]))
    q = workbench._queue
    q.put_nowait(SpectrumFrame(bytes([80]) * 512, time.monotonic()))
    service.confirmed.props["device_center_frequency"] += 1_000_000
    status = await workbench.receiver_changed()
    assert q.empty()
    assert status["watch"]["decode_target_hz"] is None
    assert status["watch"]["measurements"][0]["covered"] is False
    assert status["fresh"] is False
    await workbench.stop_watch()


@pytest.mark.asyncio
async def test_trigger_stop_does_not_stop_a_recording_it_did_not_start():
    service, workbench = _workbench()
    config = WatchConfig(frequencies_hz=[145_800_000], auto_record=True)
    await workbench.start_watch(config)
    service.recorder.state = "recording"  # an operator started a separate REC session
    await workbench.stop_watch()
    assert service.recorder.state == "recording"
    assert service.recorder.stops == 0


@pytest.mark.asyncio
async def test_unobserved_trigger_frequency_clears_quiet_timer_without_stopping_capture():
    service, workbench = _workbench()
    frequency = 145_800_000 + 300_000
    await workbench.start_watch(WatchConfig(
        frequencies_hz=[frequency], auto_record=True,
        min_active_s=0, quiet_hold_s=0.1, max_recording_s=10,
    ))
    q = workbench._queue

    def frame(center, level, at=None):
        bins = bytearray([20] * 512)
        index = round(256 + (frequency - center) / (2_000_000 / 512))
        if 0 <= index < 512:
            bins[index] = level
        return SpectrumFrame(bytes(bins), time.monotonic() if at is None else at)

    await asyncio.sleep(0.27)  # settle window after acquiring the spectrum stream
    q.put_nowait(frame(145_800_000, 150))
    for _ in range(20):
        if service.record_starts:
            break
        await asyncio.sleep(0.01)
    assert service.record_starts == 1
    q.put_nowait(frame(145_800_000, 20))
    await asyncio.sleep(0.03)
    assert workbench.trigger.status()["quiet_since"] is not None

    # The target leaves the current hardware span. A later frame reports it
    # as uncovered; the prior quiet interval must not be treated as silence.
    service.confirmed.props["device_center_frequency"] += 2_000_000
    await workbench.receiver_changed()
    await asyncio.sleep(0.3)
    q.put_nowait(frame(147_800_000, 20))
    await asyncio.sleep(0.05)
    assert workbench.trigger.status()["quiet_since"] is None
    assert service.recorder.state == "recording"
    await workbench.stop_watch()


@pytest.mark.asyncio
async def test_disconnect_invalidates_watch_and_stops_live_receiver():
    service, workbench = _workbench()
    await workbench.start_watch(WatchConfig(frequencies_hz=[145_800_000]))

    class Live:
        stopped = False

        def status(self):
            return {"frequency_hz": 145_800_000}

        async def stop(self):
            self.stopped = True

    live = Live()
    workbench.live_rx = live
    workbench._live_span = (145_800_000, 2_000_000)
    service.adapter.connected = False
    status = await workbench.receiver_changed()
    assert live.stopped
    assert workbench.live_rx is None
    assert status["watch"]["measurements"][0]["covered"] is False
    await workbench.stop_watch()


@pytest.mark.asyncio
async def test_unconfirmed_group_retune_fails_watch_without_counting_coverage():
    service, workbench = _workbench()

    async def mis_tune(rate, center, *, kind):
        assert kind == "watch_span"
        service.confirmed.props["device_sample_rate"] = rate
        service.confirmed.props["device_center_frequency"] = center + 100_000
        return True  # callback success alone is insufficient confirmation

    service._band_scan_tune_span = mis_tune
    await workbench.start_watch(
        WatchConfig(frequencies_hz=[100_000_000, 110_000_000], cycle_groups=True)
    )
    for _ in range(20):
        if not workbench.active:
            break
        await asyncio.sleep(0.01)
    assert not workbench.active
    assert "did not confirm" in workbench.error
    assert service.spectrum_hub.released == 1
