import asyncio

from msdr.engine.confirmed import ConfirmedState
from msdr.engine.dwell import DwellObserver
from msdr.radio.protocol import PropertyChanged

CONFIRMED = {
    "device_vfo_frequency": 145_800_000,
    "device_center_frequency": 145_800_000,
    "device_sample_rate": 2_000_000,
    "demodulator": "NFM",
    "filter_bandwidth": 12_500,
    "lna_state": 3,
    "active_antenna": "Antenna B",
    "squelch_enable": False,
    "squelch_threshold": -100.0,
    "agc_enable": True,
    "agc_threshold": -30.0,
    "noise_reduction_enable": False,
    "noise_reduction_strength": 0,
}

DWELL_SECONDS = 0.05


def _confirmed() -> ConfirmedState:
    cs = ConfirmedState()
    cs.seed(CONFIRMED)
    assert cs.is_complete()
    return cs


def _external(cs: ConfirmedState, prop: str, value: object) -> None:
    cs.apply(PropertyChanged(prop, value, "primary", 0.0))


async def _settle(margin: float = 3.0) -> None:
    await asyncio.sleep(DWELL_SECONDS * margin)


async def test_external_vfo_change_then_stable_fires_once_observed():
    cs = _confirmed()
    calls = []

    async def on_dwell(state, observation):
        calls.append((state, observation))

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    await observer.start()
    try:
        _external(cs, "device_vfo_frequency", 146_000_000)
        await _settle()
    finally:
        await observer.stop()

    assert len(calls) == 1
    state, _observation = calls[0]
    assert state.source == "observed"
    assert state.vfo_hz == 146_000_000


async def test_rapid_successive_changes_fire_once():
    cs = _confirmed()
    calls = []

    async def on_dwell(state, observation):
        calls.append(state)

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    await observer.start()
    try:
        for freq in (146_000_000, 146_100_000, 146_200_000):
            _external(cs, "device_vfo_frequency", freq)
            await asyncio.sleep(DWELL_SECONDS / 4)
        await _settle()
    finally:
        await observer.stop()

    assert len(calls) == 1
    assert calls[0].vfo_hz == 146_200_000


async def test_non_key_property_change_never_fires():
    cs = _confirmed()
    calls = []

    async def on_dwell(state, observation):
        calls.append(state)

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    await observer.start()
    try:
        _external(cs, "lna_state", 5)
        await _settle()
    finally:
        await observer.stop()

    assert calls == []


async def test_note_app_action_suppresses_dwell():
    # An app-driven action lands while the dwell timer is already running (but
    # before it fires): the pending observation must be suppressed even
    # though the timer itself was started by a genuine external change.
    cs = _confirmed()
    calls = []

    async def on_dwell(state, observation):
        calls.append(state)

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    await observer.start()
    try:
        _external(cs, "device_vfo_frequency", 146_000_000)
        await asyncio.sleep(DWELL_SECONDS * 0.5)
        observer.note_app_action()
        await _settle()
    finally:
        await observer.stop()

    assert calls == []


async def test_same_key_twice_fires_once():
    cs = _confirmed()
    calls = []

    async def on_dwell(state, observation):
        calls.append(state)

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    await observer.start()
    try:
        _external(cs, "device_vfo_frequency", 146_000_000)
        await _settle()
        # Retune back to the original key -- a genuinely new dwell target.
        _external(cs, "device_vfo_frequency", 145_800_000)
        await _settle()
        # Same key again: no new dwell to report.
        _external(cs, "device_vfo_frequency", 145_800_000)
        _external(cs, "device_vfo_frequency", 146_500_000)
        _external(cs, "device_vfo_frequency", 145_800_000)
        await _settle()
    finally:
        await observer.stop()

    assert len(calls) == 2
    assert calls[0].vfo_hz == 146_000_000
    assert calls[1].vfo_hz == 145_800_000


async def test_suspended_flag_blocks_firing():
    # The scanner (v0.12.0) sets this while it's driving the VFO itself.
    cs = _confirmed()
    calls = []

    async def on_dwell(state, observation):
        calls.append(state)

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    observer.suspended = True
    await observer.start()
    try:
        _external(cs, "device_vfo_frequency", 146_000_000)
        await _settle()
    finally:
        await observer.stop()

    assert calls == []


async def test_stop_cancels_pending_timer_cleanly():
    cs = _confirmed()

    async def on_dwell(state, observation):
        raise AssertionError("should not fire after stop")

    observer = DwellObserver(cs, dwell_seconds=DWELL_SECONDS, on_dwell=on_dwell)
    await observer.start()
    _external(cs, "device_vfo_frequency", 146_000_000)
    await observer.stop()
    await _settle()  # give a would-be timer time to (wrongly) fire
