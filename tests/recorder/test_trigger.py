import pytest

from msdr.recorder.trigger import ActivityTrigger, ActivityTriggerConfig


class FakeRecorder:
    def __init__(self):
        self.state = "armed"
        self.starts = 0
        self.stops = 0

    def status(self):
        return {"state": self.state}

    async def record(self):
        if self.state != "armed":
            raise RuntimeError("not armed")
        self.state = "recording"
        self.starts += 1
        return self.status()

    async def stop_recording(self):
        if self.state != "recording":
            raise RuntimeError("not recording")
        self.state = "finalizing"
        self.stops += 1
        return self.status()


@pytest.mark.asyncio
async def test_hysteresis_minimum_active_and_quiet_hold():
    now = [0.0]
    recorder = FakeRecorder()
    trigger = ActivityTrigger(
        recorder,
        ActivityTriggerConfig(
            frequency_hz=145_500_000, start_snr_db=10, stop_snr_db=5,
            min_active_s=3, quiet_hold_s=2,
        ),
        clock=lambda: now[0],
    )
    await trigger.set_enabled(True)
    assert not await trigger.observe(145_600_000, 40)
    await trigger.observe(145_500_000, 10)
    assert recorder.starts == 1

    now[0] = 1
    await trigger.observe(145_500_000, 4)
    now[0] = 2
    await trigger.observe(145_500_000, 6)  # hysteresis: still active
    assert trigger.status()["state"] == "recording"
    now[0] = 3
    await trigger.observe(145_500_000, 4)  # a new quiet interval starts
    now[0] = 4
    await trigger.tick()  # minimum duration is met, quiet hold is not
    assert recorder.stops == 0
    now[0] = 5
    await trigger.tick()
    assert recorder.stops == 1
    assert trigger.status()["owns_recording"] is False


@pytest.mark.asyncio
async def test_event_cap_locks_until_explicit_rearm():
    now = [0.0]
    recorder = FakeRecorder()
    trigger = ActivityTrigger(
        recorder,
        ActivityTriggerConfig(frequency_hz=433_000_000, quiet_hold_s=0, event_cap=1),
        clock=lambda: now[0],
    )
    await trigger.set_enabled(True)
    await trigger.observe(433_000_000, 20)
    assert trigger.status()["state"] == "recording_capped"
    now[0] = 1
    await trigger.observe(433_000_000, 0)
    await trigger.tick()
    assert recorder.stops == 1
    assert trigger.status()["state"] == "capped"
    recorder.state = "armed"  # finalization completed; Recorder accepts REC again
    await trigger.rearm()
    assert trigger.status()["state"] == "armed"
    await trigger.observe(433_000_000, 20)
    assert recorder.starts == 2


@pytest.mark.asyncio
async def test_trigger_never_stops_recording_after_external_stop():
    now = [0.0]
    recorder = FakeRecorder()
    trigger = ActivityTrigger(
        recorder,
        ActivityTriggerConfig(frequency_hz=915_000_000, quiet_hold_s=0),
        clock=lambda: now[0],
    )
    await trigger.set_enabled(True)
    await trigger.observe(915_000_000, 20)
    recorder.state = "finalizing"  # operator stop or radio failure
    now[0] = 20
    status = await trigger.tick()
    assert recorder.stops == 0
    assert status["state"] == "disabled"
    assert status["owns_recording"] is False


@pytest.mark.asyncio
async def test_duration_cap_stops_continuously_active_signal():
    now = [0.0]
    recorder = FakeRecorder()
    trigger = ActivityTrigger(
        recorder,
        ActivityTriggerConfig(frequency_hz=1_000_000, max_recording_s=2),
        clock=lambda: now[0],
    )
    await trigger.set_enabled(True)
    await trigger.observe(1_000_000, 20)
    now[0] = 2
    await trigger.tick()
    assert recorder.stops == 1


def test_invalid_hysteresis_and_cap_are_rejected():
    with pytest.raises(ValueError, match="start_snr_db"):
        ActivityTriggerConfig(frequency_hz=1, start_snr_db=4, stop_snr_db=4)
    with pytest.raises(ValueError, match="event_cap"):
        ActivityTriggerConfig(frequency_hz=1, event_cap=0)
