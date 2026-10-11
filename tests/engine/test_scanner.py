"""Scanner engine (v0.12.0): channel generation for list/range sources, the
hold-on-activity/hang/resume cycle, stop_on_hit, manual-override pause,
skip/hold controls and capture_hits dedup. Uses FakeAdapter (no network) plus
real ListRepo/SignalRepo over an in-memory Database, so channel building is
exercised against the real schema (including the 008 `skip` column).

Timings are kept in the tens of ms (dwell/settle/hang) so the whole file runs
fast; `_wait_until` polls rather than sleeping a fixed guess.
"""

import asyncio
import contextlib
import time

import pytest

from msdr.engine.confirmed import ConfirmedState
from msdr.engine.dwell import DwellObserver
from msdr.engine.fake_adapter import FakeAdapter
from msdr.engine.scanner import (
    EmptyChannelSet,
    ListSource,
    ListSourceNotFound,
    RangeSource,
    ScanConfig,
    ScannerEngine,
    ScanSourceError,
    SpectrumSource,
    TooManyChannels,
    build_channels,
    validate_spectrum_source,
)
from msdr.library.models import Signal
from msdr.radio.protocol import PropertyChanged
from msdr.radio.registry import RESTORABLE
from msdr.storage.db import Database
from msdr.storage.repos import ListRepo, SignalRepo


def _confirmed_from_adapter(adapter: FakeAdapter) -> ConfirmedState:
    cs = ConfirmedState(api_version=str(adapter.state.get("api_version", "")))
    cs.seed({spec.name: adapter.state[spec.name] for spec in RESTORABLE})
    cs.props["signal_snr"] = adapter.state.get("signal_snr")
    cs.props["signal_power"] = adapter.state.get("signal_power")
    return cs


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def repos(db):
    return ListRepo(db), SignalRepo(db)


async def _wait_until(cond, timeout: float = 2.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


def _engine(adapter, confirmed, lists, signals, *, capture=None, nearby_hz=0):
    dwell = DwellObserver(confirmed, dwell_seconds=1.0, on_dwell=lambda *a: None)

    async def _capture(recipe=None):
        if capture is not None:
            return await capture(recipe)
        raise AssertionError("capture() should not be called in this test")

    statuses: list[dict] = []
    hits: list[dict] = []

    async def on_status(status):
        statuses.append(status)

    async def on_hit(hit):
        hits.append(hit)

    engine = ScannerEngine(
        adapter,
        confirmed,
        lists,
        signals,
        dwell,
        get_nearby_hz=lambda: nearby_hz,
        capture=_capture,
        on_status=on_status,
        on_hit=on_hit,
    )
    return engine, dwell, statuses, hits


# --- channel generation -----------------------------------------------------


def test_range_channels_are_inclusive_and_labeled():
    source = RangeSource(start_hz=1_000_000, stop_hz=1_000_020, step_hz=10)
    channels = build_channels(source, lists=None, signals=None)
    assert [c.frequency_hz for c in channels] == [1_000_000, 1_000_010, 1_000_020]
    assert [c.index for c in channels] == [0, 1, 2]
    assert all(c.signal_id is None for c in channels)
    assert channels[0].label  # format_mhz(...) -- non-empty, don't pin exact text


def test_range_channel_cap_rejects_too_many():
    source = RangeSource(start_hz=0, stop_hz=10_000 * 100, step_hz=100)  # 10_001 channels
    with pytest.raises(TooManyChannels):
        build_channels(source, lists=None, signals=None)


def test_range_channel_cap_allows_exactly_the_max():
    source = RangeSource(start_hz=0, stop_hz=9_999 * 100, step_hz=100)  # exactly 10_000
    channels = build_channels(source, lists=None, signals=None)
    assert len(channels) == 10_000


def test_list_channels_preserve_order_and_skip_flag(repos):
    lists, signals = repos
    lst = lists.create("Sweep")
    a = Signal(name="A", frequency_hz=100_000_000)
    b = Signal(name="B", frequency_hz=200_000_000)
    c = Signal(name="C", frequency_hz=300_000_000)
    for s in (a, b, c):
        signals.add(s)
    lists.add_items(lst["id"], [c.id, a.id, b.id])
    lists.set_item_skip(lst["id"], a.id, True)

    channels = build_channels(ListSource(list_id=lst["id"]), lists=lists, signals=signals)

    assert [ch.label for ch in channels] == ["C", "B"]  # a skipped, order preserved
    assert [ch.index for ch in channels] == [0, 1]
    assert channels[0].signal_id == c.id


def test_list_source_unknown_list_raises(repos):
    lists, signals = repos
    with pytest.raises(ListSourceNotFound):
        build_channels(ListSource(list_id="nope"), lists=lists, signals=signals)


def test_empty_list_after_skip_raises(repos):
    lists, signals = repos
    lst = lists.create("Sweep")
    a = Signal(name="A", frequency_hz=100_000_000)
    signals.add(a)
    lists.add_items(lst["id"], [a.id])
    lists.set_item_skip(lst["id"], a.id, True)
    with pytest.raises(EmptyChannelSet):
        build_channels(ListSource(list_id=lst["id"]), lists=lists, signals=signals)


# --- hold / hang / resume, stop_on_hit --------------------------------------


async def test_hold_on_activity_then_hang_resumes(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    engine, dwell, statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]),
        dwell_ms=30,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.05,
    )
    await engine.start(config)
    assert dwell.suspended is True

    # FakeAdapter seeds signal_snr=12.0 (above the 10 dB threshold), so the
    # very first channel's dwell should trigger a hold.
    await _wait_until(lambda: engine.state == "holding")
    assert len(engine.hits) == 1
    assert engine.hits[0].peak_snr_db >= 10.0

    # Signal drops away -- after hang_s of inactivity the scan must resume.
    confirmed.props["signal_snr"] = 2.0
    await _wait_until(lambda: engine.state != "holding")
    assert engine.state == "running"
    assert engine.hits[0].duration_s > 0

    await engine.stop()
    assert engine.state == "idle"
    assert dwell.suspended is False
    assert statuses  # status callback fired at least on start/hold/resume


async def test_stop_on_hit_stops_after_the_hang_timeout(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    signals.add(a)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    engine, _dwell, _statuses, hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.03,
        stop_on_hit=True,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")

    confirmed.props["signal_snr"] = 0.0
    await _wait_until(lambda: engine.state == "idle", timeout=2.0)
    assert len(hits) == 1
    assert len(engine.hits) == 1


# --- manual override pauses the scan ----------------------------------------


async def test_manual_tune_pauses_scan(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    signals.add(a)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0  # keep it below threshold -- stay in "running"
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]),
        dwell_ms=500,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=1.0,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.channel is not None and not engine.is_tuning)

    # Not caused by the scanner (own tune already settled) -- an operator or
    # another AppService restore moving the VFO must pause the scan.
    confirmed.apply(
        PropertyChanged("device_vfo_frequency", 147_000_000, "primary", time.monotonic())
    )

    await _wait_until(lambda: engine.state == "paused")
    assert engine.reason == "manual tune"

    await engine.stop()


# --- manual controls: skip / hold -------------------------------------------


async def test_skip_advances_immediately(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]), dwell_ms=1000, settle_ms=10, threshold_snr_db=10.0
    )
    await engine.start(config)
    await _wait_until(lambda: engine.channel is not None and not engine.is_tuning)

    engine.skip()
    await _wait_until(lambda: engine.position == 1, timeout=1.0)

    await engine.stop()


async def test_skip_persist_marks_list_item_skip(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]), dwell_ms=1000, settle_ms=10, threshold_snr_db=10.0
    )
    await engine.start(config)
    await _wait_until(lambda: engine.channel is not None and not engine.is_tuning)

    engine.skip(persist=True)
    # A drops out of the active rotation too (not just the DB) -- the list
    # shrinks to just B, which lands back at position 0.
    await _wait_until(
        lambda: engine.channel is not None and engine.channel.signal_id == b.id, timeout=1.0
    )
    assert engine.position == 0 and engine.total == 1
    await engine.stop()

    assert lists.item_rows(lst["id"])[0] == {"signal_id": a.id, "skip": True}


async def test_skip_persist_removes_channel_from_active_rotation(repos):
    # Claim: skip(persist=True) updates the DB's skip flag but not the
    # running scan's own cached channel list, so a two-item list keeps
    # wrapping A back into rotation right after "skip always"-ing it.
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]), dwell_ms=30, settle_ms=10, threshold_snr_db=10.0
    )
    await engine.start(config)
    await _wait_until(lambda: engine.channel is not None and engine.channel.signal_id == a.id)

    engine.skip(persist=True)
    await _wait_until(lambda: engine.channel is not None and engine.channel.signal_id == b.id)

    assert engine.total == 1  # A dropped out of the active rotation, not just the DB
    await asyncio.sleep(0.15)  # several dwell cycles -- long enough to wrap around twice
    assert engine.channel.signal_id == b.id

    await engine.stop()


async def test_manual_hold_stays_on_current_channel(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]), dwell_ms=20, settle_ms=10, threshold_snr_db=10.0
    )
    await engine.start(config)
    await _wait_until(lambda: engine.position == 0)
    engine.toggle_hold()

    # Several dwell cycles' worth of time -- position must not move.
    await asyncio.sleep(0.15)
    assert engine.position == 0

    engine.toggle_hold()
    await _wait_until(lambda: engine.position == 1, timeout=1.0)
    await engine.stop()


# --- capture_hits dedup ------------------------------------------------------


async def test_capture_hits_creates_signal_when_nothing_nearby(repos):
    lists, signals = repos
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    calls = []

    async def capture(recipe=None):
        calls.append(True)
        return Signal(name="captured", frequency_hz=146_000_000)

    engine, _dwell, _statuses, _hits = _engine(
        adapter, confirmed, lists, signals, capture=capture, nearby_hz=500
    )
    config = ScanConfig(
        source=RangeSource(start_hz=146_000_000, stop_hz=146_000_000, step_hz=1),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.03,
        capture_hits=True,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")
    confirmed.props["signal_snr"] = 0.0
    await _wait_until(lambda: len(calls) == 1, timeout=2.0)
    await engine.stop()


async def test_capture_hit_uses_channel_recipe_not_live_receiver_state(repos):
    # Claim: a manual tune during a hold pauses the scan (_watch_overrides),
    # but the capture that follows read the *current* (just-retuned) receiver
    # state instead of the channel that actually produced the hit.
    lists, signals = repos
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    recipes = []

    async def capture(recipe=None):
        recipes.append(recipe)
        return Signal(name="captured", frequency_hz=recipe.frequency_hz)

    engine, _dwell, _statuses, _hits = _engine(
        adapter, confirmed, lists, signals, capture=capture, nearby_hz=0
    )
    config = ScanConfig(
        source=RangeSource(start_hz=146_000_000, stop_hz=146_000_000, step_hz=1),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=1.0,  # long enough that only the manual override ends the hold
        capture_hits=True,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")

    # Operator retunes away mid-hold -- ends the hold via manual-override
    # pause, well before hang_s would.
    confirmed.apply(
        PropertyChanged("device_vfo_frequency", 146_500_000, "primary", time.monotonic())
    )
    await _wait_until(lambda: len(recipes) == 1, timeout=2.0)
    assert recipes[0].frequency_hz == 146_000_000  # the channel's, not the manual retune's

    await engine.stop()


# --- spectrum source (v0.13.0) -----------------------------------------------


async def test_spectrum_candidate_detected_tuned_and_locked_out(repos):
    lists, signals = repos
    adapter = FakeAdapter()
    target = 145_787_500  # snapped to the 12_500 Hz grid
    adapter.spectrum_peaks = {target: 160}

    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, hits = _engine(adapter, confirmed, lists, signals)

    # SNR follows the VFO: high on the peak, low everywhere else -- like the
    # real receiver would report, and required so the hit actually proves the
    # scanner tuned to the right frequency rather than just holding anywhere.
    async def _snr_watcher():
        q = adapter.subscribe()
        try:
            while True:
                ev = await q.get()
                if isinstance(ev, PropertyChanged) and ev.property == "device_vfo_frequency":
                    confirmed.props["signal_snr"] = 20.0 if ev.value == target else 0.0
        finally:
            adapter.unsubscribe(q)

    watcher = asyncio.create_task(_snr_watcher())
    try:
        config = ScanConfig(
            source=SpectrumSource(level_threshold=20),
            dwell_ms=30,
            settle_ms=10,
            threshold_snr_db=10.0,
            hang_s=0.05,
        )
        await engine.start(config)

        await _wait_until(lambda: engine.state == "holding", timeout=2.0)
        assert len(hits) == 1
        assert hits[0]["frequency_hz"] == target
        assert hits[0]["label"] == "spectrum 145.787500 MHz"
        assert engine.channel is not None and engine.channel.frequency_hz == target

        confirmed.props["signal_snr"] = 0.0
        await _wait_until(lambda: engine.state != "holding", timeout=2.0)

        # Lockout: the only peak in the spectrum was just visited, so an
        # immediate re-sweep must not hold there again.
        await asyncio.sleep(0.2)
        assert len(hits) == 1

        await engine.stop()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


async def test_spectrum_wide_range_steps_the_centre(repos):
    lists, signals = repos
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0  # no peaks -- never holds
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        # Seeded center/sample_rate: 145_800_000 / 2_000_000. Range wider than
        # the span forces segment stepping (0.8 * 2_000_000 = 1_600_000/step).
        source=SpectrumSource(start_hz=144_000_000, stop_hz=148_000_000, level_threshold=20),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.05,
    )
    await engine.start(config)

    # 145.8 + 1.6 would overshoot, so the last segment is pinned at
    # stop_hz - step/2 = 147.2 MHz, keeping stop_hz visible.
    await _wait_until(
        lambda: confirmed.props["device_center_frequency"] == 147_200_000, timeout=2.0
    )
    # From the last segment the walk wraps back to the first one, centred so
    # start_hz is inside the span (144_000_000 + 800_000).
    await _wait_until(
        lambda: confirmed.props["device_center_frequency"] == 144_800_000, timeout=2.0
    )
    await engine.stop()


async def test_spectrum_range_outside_current_span_moves_centre_into_it(repos):
    # Regression from the real radio: parked on 162 MHz, a spectrum scan of
    # the FM band never found a candidate because nothing moved the centre.
    lists, signals = repos
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)

    # Fits in one 2 MHz span but lies entirely outside the seeded 145.8 MHz one.
    config = ScanConfig(
        source=SpectrumSource(start_hz=98_000_000, stop_hz=99_000_000, level_threshold=20),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.05,
    )
    await engine.start(config)
    await _wait_until(lambda: confirmed.props["device_center_frequency"] == 98_500_000, timeout=2.0)
    await engine.stop()

    # A wide range outside the span jumps to its first segment.
    engine2, *_ = _engine(adapter, confirmed, lists, signals)
    wide = ScanConfig(
        source=SpectrumSource(start_hz=88_000_000, stop_hz=94_000_000, level_threshold=20),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.05,
    )
    await engine2.start(wide)
    await _wait_until(lambda: confirmed.props["device_center_frequency"] == 88_800_000, timeout=2.0)
    await engine2.stop()


async def test_spectrum_excludes_dc_and_edge_bins(repos):
    lists, signals = repos
    adapter = FakeAdapter()
    center, sample_rate = 145_800_000, 2_000_000
    bin_hz = sample_rate / 512
    edge_hz = int(center - sample_rate / 2 + 5 * bin_hz)  # bin 5: outer 3%
    adapter.spectrum_peaks = {edge_hz: 200}

    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine, _dwell, _statuses, hits = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=SpectrumSource(level_threshold=20),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.05,
    )
    await engine.start(config)
    await asyncio.sleep(0.3)  # several spectrum cycles' worth

    assert hits == []
    assert engine.channel is None or engine.channel.frequency_hz != edge_hz
    await engine.stop()


def test_spectrum_source_rejects_start_at_or_after_stop():
    with pytest.raises(ScanSourceError):
        validate_spectrum_source(SpectrumSource(start_hz=146_000_000, stop_hz=146_000_000))


async def test_capture_hits_skips_when_a_memory_is_already_nearby(repos):
    lists, signals = repos
    signals.add(Signal(name="Existing", frequency_hz=146_000_100))  # within nearby_hz below

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    calls = []

    async def capture(recipe=None):
        calls.append(True)
        raise AssertionError("must not capture when a memory is already nearby")

    engine, _dwell, _statuses, _hits = _engine(
        adapter, confirmed, lists, signals, capture=capture, nearby_hz=500
    )
    config = ScanConfig(
        source=RangeSource(start_hz=146_000_000, stop_hz=146_000_000, step_hz=1),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.03,
        capture_hits=True,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")
    confirmed.props["signal_snr"] = 0.0
    await _wait_until(lambda: len(engine.hits) == 1 and engine.state != "holding", timeout=2.0)
    assert calls == []
    await engine.stop()


async def test_all_channels_ignored_keeps_yielding_and_stops(repos):
    # Review 2026-09-26: the ignored-channel branch returned without an await,
    # so a list of only ignored finds spun the scan loop and starved the event
    # loop -- STOP (an API call) could never run.
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Ignored")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    engine, _dwell, _statuses, _hits = _engine(adapter, confirmed, lists, signals)
    engine._is_ignored = lambda _hz: True
    config = ScanConfig(
        source=ListSource(list_id=lst["id"]), dwell_ms=1000, settle_ms=10, threshold_snr_db=10.0
    )
    await engine.start(config)

    # The event loop stays responsive while the scan runs...
    t0 = time.monotonic()
    await asyncio.sleep(0.2)
    assert time.monotonic() - t0 < 1.0
    assert engine.skipped_ignored >= 2  # a whole pass was skipped, none tuned
    assert adapter.state.get("device_vfo_frequency") not in (146_000_000, 146_100_000)
    # ...and STOP gets through.
    await asyncio.wait_for(engine.stop(), 2.0)
    assert engine.state == "idle"
