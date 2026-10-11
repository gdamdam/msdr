"""Analyzer: round-robin over targets with a fake IQ stream, same pump
pattern as tests/decoder's BandDecoder fakes -- a frames() async generator
yielding synthesized 8 MS/s-style frames (scaled down here for test speed;
the channelizer/pump maths don't care about the absolute rate)."""

import asyncio
import contextlib
import types

import numpy as np
import pytest

from msdr.contracts.source import SourceDescriptor
from msdr.recorder.iq_stream import IQFrame
from msdr.settings import RFMLSettings
from msdr.source.base import SourceEvent
from msdr.survey.analyzer import Analyzer, _TargetState

_SAMPLE_RATE_HZ = 200_000
_SPACING_HZ = 12_500


class FakeIQFrame:
    def __init__(self, data: bytes):
        self.data = data


class FakeIQStream:
    """IQStream double: pre-baked frames, paced by `frame_dt` so the pump
    task (a real asyncio.to_thread call per frame) can keep up -- like the
    real stream, frames() ends the connection when exhausted rather than
    hanging forever."""

    def __init__(self, frames: list[bytes], frame_dt: float):
        self._frames = frames
        self._frame_dt = frame_dt
        self.started = False
        self.enabled = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def enable(self, on: bool) -> None:
        self.enabled = on

    async def frames(self):
        for data in self._frames:
            yield FakeIQFrame(data)
            await asyncio.sleep(self._frame_dt)


def _iq_to_frame_bytes(iq: np.ndarray) -> bytes:
    i = np.clip(iq.real, -1.0, 1.0)
    q = np.clip(iq.imag, -1.0, 1.0)
    interleaved = np.empty(iq.size * 2, dtype=np.int16)
    interleaved[0::2] = (i * 32767).astype(np.int16)
    interleaved[1::2] = (q * 32767).astype(np.int16)
    return interleaved.tobytes()


class _ReplayQueueStream:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def start(self): ...
    async def stop(self): ...
    async def enable(self, _on): ...

    async def frames(self):
        while True:
            yield await self.queue.get()


class _ReplaySource:
    source_id = "replay:analyzer-test"

    def __init__(self):
        self._stream = _ReplayQueueStream()
        self._listeners = []
        self._desc = SourceDescriptor(
            source_id=self.source_id,
            mode="replay",
            sample_rate_hz=2_000_000,
            center_hz=100_000_000,
        )

    def descriptor(self):
        return self._desc

    def iq_stream(self):
        return self._stream

    def add_listener(self, listener):
        self._listeners.append(listener)

    def remove_listener(self, listener):
        self._listeners.remove(listener)

    def seek(self):
        self._desc = self._desc.bump("seek").model_copy(update={"sample_index": 10_000})
        event = SourceEvent("generation", self._desc.generation, 10_000, 0, self._desc)
        for listener in list(self._listeners):
            listener(event)


async def test_replay_generation_flushes_analyzer_queue_and_drops_late_frames():
    source = _ReplaySource()
    analyzer = Analyzer(
        iq_stream_factory=source.iq_stream,
        center_hz=100_000_000,
        sample_rate_hz=2_000_000,
        max_analyzed=2,
        dwell_s=0.1,
        min_snr_db=8.0,
        on_result=lambda *_: None,
        on_status=lambda: None,
    )
    await source._stream.start()
    source.add_listener(analyzer._on_source_event)
    analyzer._stream = source._stream
    reader = asyncio.create_task(analyzer._read_frames())
    old = IQFrame(bytes(4096), 0.0, generation=0, sample_index=0)
    await source._stream.queue.put(old)
    for _ in range(20):
        if analyzer._queue.qsize():
            break
        await asyncio.sleep(0)
    assert analyzer._queue.qsize() == 1

    old_retune_gen = analyzer._retune_gen
    source.seek()
    assert analyzer._queue.empty()
    assert analyzer._retune_gen == old_retune_gen + 1

    # A pre-seek frame still waiting in the source stream must not be copied
    # into the analyzer's post-seek queue.
    await source._stream.queue.put(old)
    for _ in range(20):
        await asyncio.sleep(0)
    assert analyzer._queue.empty()

    new = IQFrame(bytes(4096), 0.0, generation=1, sample_index=10_000)
    await source._stream.queue.put(new)
    for _ in range(20):
        if analyzer._queue.qsize():
            break
        await asyncio.sleep(0)
    assert analyzer._queue.get_nowait() is new

    reader.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reader
    source.remove_listener(analyzer._on_source_event)
    await source._stream.stop()


def _tone_frames(freq_hz: float, total_raw: int, chunk: int) -> list[bytes]:
    t = np.arange(total_raw) / _SAMPLE_RATE_HZ
    raw = (0.5 * np.exp(1j * 2 * np.pi * freq_hz * t)).astype(np.complex64)
    return [_iq_to_frame_bytes(raw[i : i + chunk]) for i in range(0, total_raw, chunk)]


def _bursty_fsk_frames(
    freq_hz: float,
    total_raw: int,
    chunk: int,
    baud: float = 1200.0,
    dev_hz: float = 2400.0,
    on_s: float = 0.04,
    off_s: float = 0.02,
    seed: int = 1,
) -> list[bytes]:
    """2-FSK centred on `freq_hz`, keyed on/off on an (on_s, off_s) duty
    cycle -- unlike `_tone_frames`'s always-on bare carrier, this gives the
    analyzer's activity gate (see analyzer.py's _gate_active_blocks) real
    contrast to find a per-target noise floor from (the way an actual
    bursty/intermittent S2 target does), while still classifying as
    "fm_fsk" -- needed for want_voice's fm_fsk/unknown gate. Defaults are
    multiples of the gate's own 20 ms block size (on=2 blocks, off=1 block)
    so no block straddles an on/off transition -- a straddling block reads
    as weakly "on" instead of clearly "off", which collapses the measured
    contrast between blocks well below _ACTIVITY_MARGIN_DB even though the
    underlying signal really is >99% attenuated when off."""
    rng = np.random.default_rng(seed)
    t = np.arange(total_raw) / _SAMPLE_RATE_HZ
    n_syms = int(total_raw / (_SAMPLE_RATE_HZ / baud)) + 2
    bits = rng.integers(0, 2, n_syms)
    boundaries = np.round(np.arange(n_syms + 1) * (_SAMPLE_RATE_HZ / baud)).astype(int)
    freq_per_sample = np.zeros(boundaries[-1], dtype=np.float64)
    for i in range(n_syms):
        freq_per_sample[boundaries[i] : boundaries[i + 1]] = dev_hz if bits[i] else -dev_hz
    freq_per_sample = freq_per_sample[:total_raw]
    phase = 2 * np.pi * np.cumsum(freq_per_sample) / _SAMPLE_RATE_HZ

    cycle_s = on_s + off_s
    on = np.mod(t, cycle_s) < on_s
    envelope = np.where(on, 1.0, 0.01)
    raw = (0.5 * envelope * np.exp(1j * (phase + 2 * np.pi * freq_hz * t))).astype(np.complex64)
    return [_iq_to_frame_bytes(raw[i : i + chunk]) for i in range(0, total_raw, chunk)]


def _bursty_fsk_iq(freq_hz: float, total_raw: int, seed: int = 1) -> np.ndarray:
    """Same signal as `_bursty_fsk_frames`, as a raw complex array (not yet
    chunked into frame bytes) -- lets a test sum several independent
    emitters into one wideband stream, e.g. one per round-robin target."""
    baud, dev_hz, on_s, off_s = 1200.0, 2400.0, 0.04, 0.02
    rng = np.random.default_rng(seed)
    t = np.arange(total_raw) / _SAMPLE_RATE_HZ
    n_syms = int(total_raw / (_SAMPLE_RATE_HZ / baud)) + 2
    bits = rng.integers(0, 2, n_syms)
    boundaries = np.round(np.arange(n_syms + 1) * (_SAMPLE_RATE_HZ / baud)).astype(int)
    freq_per_sample = np.zeros(boundaries[-1], dtype=np.float64)
    for i in range(n_syms):
        freq_per_sample[boundaries[i] : boundaries[i + 1]] = dev_hz if bits[i] else -dev_hz
    freq_per_sample = freq_per_sample[:total_raw]
    phase = 2 * np.pi * np.cumsum(freq_per_sample) / _SAMPLE_RATE_HZ

    cycle_s = on_s + off_s
    on = np.mod(t, cycle_s) < on_s
    envelope = np.where(on, 1.0, 0.01)
    return (0.35 * envelope * np.exp(1j * (phase + 2 * np.pi * freq_hz * t))).astype(np.complex64)


def _two_target_frames(freq1_hz: float, freq2_hz: float, total_raw: int, chunk: int) -> list[bytes]:
    """Two independent bursty-FSK emitters summed into one wideband stream
    -- unlike feeding a single emitter and reading the second target off its
    filter leakage (too weak for the activity gate to reliably find contrast
    in), each target here has its own real signal, matching the round-robin
    test's actual intent."""
    raw = _bursty_fsk_iq(freq1_hz, total_raw, seed=1) + _bursty_fsk_iq(freq2_hz, total_raw, seed=2)
    return [_iq_to_frame_bytes(raw[i : i + chunk]) for i in range(0, total_raw, chunk)]


def _make_analyzer(factory, *, dwell_s=0.05, max_analyzed=6, want_voice=None, min_active_s=0.6):
    results: list[tuple[int, dict]] = []
    statuses = []
    return (
        Analyzer(
            iq_stream_factory=factory,
            center_hz=0,
            sample_rate_hz=_SAMPLE_RATE_HZ,
            max_analyzed=max_analyzed,
            dwell_s=dwell_s,
            min_snr_db=8.0,
            on_result=lambda freq, fields: results.append((freq, fields)),
            on_status=lambda: statuses.append(None),
            spacing_hz=_SPACING_HZ,
            want_voice=want_voice,
            min_active_s=min_active_s,
        ),
        results,
        statuses,
    )


async def test_both_ml_worker_callbacks_accept_current_results():
    results = []
    settings = RFMLSettings(enabled=True)
    settings.detector.enabled = True
    analyzer = Analyzer(
        iq_stream_factory=lambda: FakeIQStream([], 0),
        center_hz=0,
        sample_rate_hz=_SAMPLE_RATE_HZ,
        max_analyzed=1,
        dwell_s=0.05,
        min_snr_db=8.0,
        on_result=lambda _freq, _fields: None,
        on_status=lambda: None,
        ml_settings=settings,
        on_ml_result=lambda metadata, result: results.append((metadata, result)),
    )
    analyzer.set_targets([12_500])

    def assert_callbacks():
        metadata = {"analyzer_epoch": analyzer._retune_gen, "frequency_hz": 12_500}
        analyzer._ml.on_result(metadata, {"class": "fm"})
        analyzer._ml_detector.on_result(
            {"analyzer_epoch": analyzer._retune_gen}, {"detections": [1]}
        )
        assert results[-1][1]["class"] == "fm"
        assert analyzer._ml_detector_result["detections"] == [1]

    assert_callbacks()  # constructor-created workers
    await analyzer.configure_ml(RFMLSettings())
    await analyzer.configure_ml(settings)
    assert_callbacks()  # live-created workers


class _MLStub:
    """Stands in for RFMLWorker: just enough to flip the pump's ML branch
    on without spinning up the real (heavy) worker."""

    def invalidate(self) -> None:
        pass

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def test_ml_gap_does_not_reset_dsp_accumulator():
    # Review 2026-09-28: with an ML worker enabled, a dropped IQ frame reset
    # the DSP dwell accumulator too, so under real (lossy) delivery rates the
    # accumulator never reached `needed` and DSP results never completed.
    # Only the ML capture window may be invalidated on a gap.
    target = 12500
    chunk = 2048
    total_raw = 200_000  # ~1.0 s of signal, paced close to real time below
    frames = _tone_frames(target, total_raw, chunk)
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream(frames, chunk / _SAMPLE_RATE_HZ),
        dwell_s=5.0,  # never completes within this test's ~1 s of signal
    )
    analyzer.set_targets([target])
    analyzer._ml = _MLStub()  # enable the pump's "ML worker active" branch

    reset_calls = []
    real_reset_accum = analyzer._reset_accum
    analyzer._reset_accum = lambda: (reset_calls.append(None), real_reset_accum())

    await analyzer.start()
    await asyncio.sleep(0.3)
    accum_before_gap = analyzer._accum_len
    assert accum_before_gap > 0
    resets_before_gap = len(reset_calls)

    analyzer._dropped += 1  # simulate a dropped IQ frame / queue overflow
    await asyncio.sleep(0.3)
    await analyzer.stop()

    assert len(reset_calls) == resets_before_gap  # gap must not reset the DSP accumulator
    assert analyzer._accum_len > accum_before_gap  # accumulation kept going across the gap


async def test_analyzer_produces_one_result_per_target_and_round_robins():
    # Channel spacing is 12.5 kHz -> +-12500 Hz sit in two distinct channels
    # (k=1 and k=15/-1) either side of a center_hz=0.
    target1, target2 = 12500, -12500
    total_raw = 400_000  # 2.0 s -- generous margin for the activity gate to converge
    chunk = 2048
    frame_dt = chunk / _SAMPLE_RATE_HZ
    # Each target is its own bursty (not bare-carrier) emitter, summed into
    # one wideband stream -- see _two_target_frames. The activity gate needs
    # on/off contrast within a dwell to find a real per-target noise floor;
    # dwell_s=0.2 gives it 10 blocks/dwell (20 ms blocks) instead of 2 --
    # enough for the 10th-percentile floor to mean something.
    frames = _two_target_frames(target1, target2, total_raw, chunk)

    factory_calls = []

    def factory():
        factory_calls.append(1)
        return FakeIQStream(frames, frame_dt)

    analyzer, results, _statuses = _make_analyzer(factory, dwell_s=0.2, min_active_s=0.05)
    analyzer.set_targets([target1, target2])

    await analyzer.start()
    await asyncio.sleep(total_raw / _SAMPLE_RATE_HZ + 1.0)
    await analyzer.stop()

    assert len(factory_calls) == 1
    # Only dwells past the activity gate (not "waiting for signal") count --
    # round-robins: both targets eventually get a real analysis (within
    # +-1 channel spacing/2 of each; results are keyed by the target, the
    # PSD-centroid-refined frequency rides along as fields["measured_hz"]).
    analyzed = [(freq, fields) for freq, fields in results if fields["analyzed_at"] is not None]
    assert len(analyzed) >= 2
    seen_targets = set()
    for frequency_hz, fields in analyzed:
        nearest = min((target1, target2), key=lambda t: abs(t - frequency_hz))
        assert abs(frequency_hz - nearest) <= _SPACING_HZ / 2
        seen_targets.add(nearest)
        assert fields["modulation"]["kind"] in (
            "carrier",
            "am",
            "fm_fsk",
            "ook",
            "psk_like",
            "unknown",
        )
    assert seen_targets == {target1, target2}


async def test_analyzer_status_reports_queue_and_analyzing():
    target1, target2, target3 = 12500, -12500, 25000
    frames = _tone_frames(target1, 20_000, 2048)
    analyzer, _results, _statuses = _make_analyzer(lambda: FakeIQStream(frames, 0.001), dwell_s=1.0)
    analyzer.set_targets([target1, target2, target3])

    status = analyzer.status()
    assert status["analyzing"] == target1
    assert status["queue"] == [target2, target3]
    assert status["max_analyzed"] == 6
    assert status["dwell_s"] == 1.0
    assert status["active"] is False  # start() not called yet
    assert status["iq"] == {
        "connected": False,
        "frames": 0,
        "dropped": 0,
        "rate_hz": None,
        "cpu_load": 0.0,
    }


async def test_analyzer_set_targets_keeps_progress_on_surviving_target():
    target1, target2 = 12500, -12500
    frames = _tone_frames(target1, 20_000, 2048)
    analyzer, _results, _statuses = _make_analyzer(lambda: FakeIQStream(frames, 0.001))
    analyzer.set_targets([target1, target2])
    assert analyzer.status()["analyzing"] == target1

    analyzer.set_targets([target2, target1])  # reordered, both still present
    assert analyzer.status()["analyzing"] == target1  # unchanged: still in the list

    analyzer.set_targets([target2])  # target1 missed one frame: dwell kept
    assert analyzer.status()["analyzing"] == target1
    analyzer._target_seen[target1] -= 60.0  # ...and then gone past the grace period
    analyzer.set_targets([target2])
    assert analyzer.status()["analyzing"] == target2


def test_tuned_target_stays_active_while_other_candidates_rotate_after_dwells():
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), max_analyzed=3
    )
    tuned = 12_500
    others = [-90_000, -60_000, -30_000, 30_000, 60_000, 90_000]
    candidates = [tuned, *others]
    analyzer.set_targets(candidates, priority_frequency_hz=tuned)
    assert analyzer.status()["analyzing"] == tuned
    assert analyzer.status()["targets"][0]["frequency_hz"] == tuned

    # Frequent spectrum refreshes, including centroid jitter, cannot advance
    # the page or erase progress in an unfinished dwell.
    analyzer._accum = [np.zeros(1000, dtype=np.complex64)]
    analyzer._accum_len = 1000
    for jitter in (300, -300, 400, 0):
        analyzer.set_targets([tuned, *[f + jitter for f in others]],
                             priority_frequency_hz=tuned)
        assert analyzer._current_freq == tuned
        assert analyzer._accum_len == 1000
        assert len(analyzer.status()["targets"]) <= 3

    visited = set()
    for dwell in range(15):
        visited.add(analyzer._current_freq)
        analyzer._advance_target()  # one completed dwell
        # SNR ranks can reverse between frames; the rotation must still
        # eventually visit every frequency.
        incoming = [tuned, *(reversed(others) if dwell % 2 else others)]
        analyzer.set_targets(incoming, priority_frequency_hz=tuned)
        assert len(analyzer.status()["targets"]) <= 3
        assert tuned in [t["frequency_hz"] for t in analyzer.status()["targets"]]
    assert visited == set(candidates)


def test_vfo_change_interrupts_old_dwell_and_single_slot_still_rotates():
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), max_analyzed=1
    )
    old_vfo, new_vfo, weak_a, weak_b = 12_500, 14_000, -40_000, 40_000
    analyzer.set_targets([old_vfo, weak_a, weak_b], priority_frequency_hz=old_vfo)
    analyzer._accum = [np.zeros(1000, dtype=np.complex64)]
    analyzer._accum_len = 1000
    analyzer.set_targets([new_vfo, weak_a, weak_b], priority_frequency_hz=new_vfo)
    assert analyzer.status()["analyzing"] == new_vfo
    assert analyzer._accum_len == 0
    assert analyzer._narrow_key[0] == new_vfo

    visited = []
    for _ in range(6):
        visited.append(analyzer._current_freq)
        analyzer._advance_target()
    assert visited == [new_vfo, weak_a, new_vfo, weak_b, new_vfo, weak_a]


def test_completed_ml_result_is_accepted_after_target_pages_out():
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), max_analyzed=1
    )
    received = []
    analyzer._on_ml_result = lambda metadata, result: received.append((metadata, result))
    first, second = -40_000, 40_000
    analyzer.set_targets([first, second])
    analyzer._advance_target()
    assert first not in [t["frequency_hz"] for t in analyzer.status()["targets"]]
    analyzer._accept_ml_result(
        {"frequency_hz": first, "analyzer_epoch": analyzer._retune_gen},
        {"classification": "FM"},
    )
    assert len(received) == 1


def test_graceful_dwell_can_finish_after_all_candidates_disappear():
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), max_analyzed=2
    )
    target = 12_500
    analyzer.set_targets([target])
    analyzer.set_targets([])  # S2 track briefly vanished mid-dwell
    assert analyzer._current_freq == target
    analyzer._advance_target()
    assert analyzer.status()["analyzing"] is None
    assert analyzer.status()["targets"] == []


def test_target_active_seconds_use_narrow_channel_sample_rate():
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), max_analyzed=1
    )
    target = 12_500
    analyzer.set_targets([target])
    state = _TargetState()
    state.active_samples = 50_000
    analyzer._target_state[target] = state
    assert analyzer.status()["targets"][0]["active_s"] == 1.0


async def test_activity_gate_silence_only_waits_for_signal():
    # Homogeneous noise: no block ever clears the persisted floor by
    # _ACTIVITY_MARGIN_DB, so the target never accumulates active audio --
    # this is the exact field bug (every S2 target on real VHF land-mobile
    # traffic came back "unknown"/16666.67 Hz on a dwell that was mostly
    # silence between bursts).
    target = 12500
    analyzer, _results, _statuses = _make_analyzer(lambda: FakeIQStream([], 0.01), dwell_s=0.05)
    analyzer.set_targets([target])
    out_rate = analyzer._channelizer.out_rate

    rng = np.random.default_rng(1)
    noise = (rng.normal(0.0, 1e-3, 2000) + 1j * rng.normal(0.0, 1e-3, 2000)).astype(np.complex64)

    frequency_hz, fields = analyzer._analyze_target(noise, out_rate)
    assert frequency_hz == target
    assert fields["note"] == "waiting for signal"
    assert fields["modulation"]["kind"] == "unknown"
    assert fields["modulation"]["confidence"] == 0.0
    assert fields["modulation"]["features"]["active_fraction"] == 0.0
    assert fields["modulation"]["features"]["active_s"] == 0.0
    assert fields["symbol_rate_hz"] is None
    assert fields["family"] is None
    assert fields["voice"] is None
    assert fields["analyzed_at"] is None

    # Stays in rotation, reported with attempts and analyzed=False.
    assert analyzer._targets == [target]
    status = analyzer.status()
    target_status = next(t for t in status["targets"] if t["frequency_hz"] == target)
    assert target_status["attempts"] == 1
    assert target_status["analyzed"] is False


async def test_activity_gate_accumulates_burst_across_visits_then_analyzes():
    # Each dwell carries one active (tone) block and one quiet block, so the
    # per-dwell 10th-percentile noise floor is legitimate (a real minority of
    # each dwell is genuinely quiet) -- min_active_s is set low enough that
    # it takes exactly two visits' worth of active blocks to cross it,
    # mirroring the spec's "burst of 0.8s...across two visits" case.
    target = 12500
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), dwell_s=0.05, min_active_s=0.03
    )
    analyzer.set_targets([target])
    out_rate = analyzer._channelizer.out_rate
    block_len = 500  # 20 ms @ 25 kHz out_rate

    t = np.arange(block_len) / out_rate
    tone = (0.5 * np.exp(1j * 2 * np.pi * 1000.0 * t)).astype(np.complex64)
    rng = np.random.default_rng(2)
    silence = (rng.normal(0.0, 1e-4, block_len) + 1j * rng.normal(0.0, 1e-4, block_len)).astype(
        np.complex64
    )
    dwell_buf = np.concatenate([tone, silence])

    freq1, fields1 = analyzer._analyze_target(dwell_buf, out_rate)
    assert freq1 == target
    assert fields1["note"] == "waiting for signal"

    freq2, fields2 = analyzer._analyze_target(dwell_buf, out_rate)
    assert freq2 == target  # keyed by the target (Survey matches labels to it)...
    assert fields2["measured_hz"] != target  # ...the PSD-centroid-refined value rides along
    assert "note" not in fields2
    assert fields2["analyzed_at"] is not None
    assert fields2["modulation"]["kind"] in (
        "carrier",
        "am",
        "fm_fsk",
        "ook",
        "psk_like",
        "unknown",
    )

    status = analyzer.status()
    target_status = next(t for t in status["targets"] if t["frequency_hz"] == target)
    assert target_status["attempts"] == 2
    assert target_status["analyzed"] is True


async def test_analyzer_want_voice_adds_voice_field():
    target = 12500
    # 1.6 s of signal at 75% duty (see _bursty_fsk_frames) accumulates
    # ~1.2 s of active audio -- comfortably past both min_active_s (0.6,
    # the default) and voice_score's own 0.5 s floor.
    total_raw = 320_000
    chunk = 2048
    frame_dt = chunk / _SAMPLE_RATE_HZ
    frames = _bursty_fsk_frames(target, total_raw, chunk)

    analyzer, results, _statuses = _make_analyzer(
        lambda: FakeIQStream(frames, frame_dt),
        dwell_s=0.2,  # 10 blocks/dwell -- see the round-robin test's comment
        want_voice=lambda: True,
        min_active_s=0.6,
    )
    analyzer.set_targets([target])

    await analyzer.start()
    await asyncio.sleep(total_raw / _SAMPLE_RATE_HZ + 1.0)
    await analyzer.stop()

    # Skip any early "waiting for signal" dwells -- voice only appears once
    # the gate has let a real analysis through.
    analyzed = [fields for _frequency_hz, fields in results if fields["analyzed_at"] is not None]
    assert analyzed
    assert analyzed[0]["voice"] is not None
    assert analyzed[0]["voice"]["kind"] in ("voice", "data", "silence")


# -- RDS dwells ------------------------------------------------------------------

_RDS_FS = 2_000_000
_RDS_CENTER_HZ = 98_000_000
_RDS_STATION_HZ = 98_300_000


def _rds_analyzer(frames, *, want_rds=True, dwell_s=2.4):
    results: list = []
    analyzer = Analyzer(
        iq_stream_factory=lambda: FakeIQStream(frames, 0.02),
        center_hz=_RDS_CENTER_HZ,
        sample_rate_hz=_RDS_FS,
        max_analyzed=6,
        dwell_s=2.0,
        min_snr_db=8.0,
        on_result=lambda freq, fields: results.append((freq, fields)),
        on_status=lambda: None,
        want_rds=lambda: want_rds,
        rds_dwell_s=dwell_s,
    )
    return analyzer, results


def _rds_frames(seconds: float) -> list[bytes]:
    from msdr.survey.rds import encode_rds_mpx, fm_modulate_chunks

    mpx = encode_rds_mpx("KTEST", 0x1234, "Now playing", seconds, 250_000)
    offset = _RDS_STATION_HZ - _RDS_CENTER_HZ
    return [
        _iq_to_frame_bytes(chunk)
        for chunk in fm_modulate_chunks(mpx, 250_000, _RDS_FS, offset, 100_000, amplitude=0.3)
    ]


async def test_analyzer_rds_dwell_decodes_station_name():
    frames = _rds_frames(2.6)
    analyzer, results = _rds_analyzer(frames)
    # Label centroid a few kHz off the carrier, as S2's 512-bin spectrum gives it.
    label_hz = _RDS_STATION_HZ + 6_000
    analyzer.set_rds_targets([label_hz])

    await analyzer.start()
    for _ in range(300):
        await asyncio.sleep(0.02)
        if any(f.get("rds", {}).get("ps") for _freq, f in results):
            break
    status = analyzer.status()
    await analyzer.stop()

    rds_results = [(freq, f) for freq, f in results if "rds" in f]
    assert rds_results
    freq, fields = rds_results[-1]
    assert freq == _RDS_STATION_HZ  # reported at the FM channel centre, not the label
    assert fields["rds"]["ps"] == "KTEST"
    assert fields["rds"]["pi"] == "0x1234"
    assert status["rds"]["block_ms"] > 0.0


async def test_analyzer_rds_idle_when_not_wanted():
    frames = _rds_frames(0.5)
    analyzer, results = _rds_analyzer(frames, want_rds=False, dwell_s=0.2)
    analyzer.set_rds_targets([_RDS_STATION_HZ])
    await analyzer.start()
    await asyncio.sleep(0.5)
    await analyzer.stop()
    assert not [f for _freq, f in results if "rds" in f]


def test_analyzer_rds_pauses_under_cpu_load():
    analyzer, _results = _rds_analyzer([])
    analyzer.set_rds_targets([_RDS_STATION_HZ])
    iq = np.zeros(100_000, dtype=np.complex64)
    analyzer._cpu_load = 0.9
    analyzer._process_rds(iq)
    assert analyzer.status()["rds"]["paused"] is True
    assert analyzer._rds_rx is None
    analyzer._cpu_load = 0.65  # between the resume and pause marks: stays paused
    analyzer._process_rds(iq)
    assert analyzer._rds_rx is None
    analyzer._cpu_load = 0.1
    analyzer._process_rds(iq)
    assert analyzer.status()["rds"]["paused"] is False
    assert analyzer._rds_rx is not None


def test_analyzer_rds_one_station_key_per_fm_channel():
    analyzer, _results = _rds_analyzer([])
    iq = np.zeros(100_000, dtype=np.complex64)
    # Two labels of one station (centroid jitter / a split peak).
    analyzer.set_rds_targets([_RDS_STATION_HZ + 43_000, _RDS_STATION_HZ - 6_000])
    analyzer._process_rds(iq)
    assert analyzer._rds_current == _RDS_STATION_HZ
    rx = analyzer._rds_rx
    # The label moves within the channel: same dwell, same receiver.
    analyzer.set_rds_targets([_RDS_STATION_HZ + 12_000])
    analyzer._process_rds(iq)
    assert analyzer._rds_rx is rx
    assert list(analyzer._rds_stations) == [_RDS_STATION_HZ]


def test_analyzer_rds_fresh_receiver_after_dropped_frames():
    analyzer, _results = _rds_analyzer([])
    analyzer.set_rds_targets([_RDS_STATION_HZ])
    iq = np.zeros(100_000, dtype=np.complex64)
    analyzer._process_rds(iq)
    rx = analyzer._rds_rx
    analyzer._process_rds(iq)
    assert analyzer._rds_rx is rx
    analyzer._dropped += 1  # the IQ pump dropped a frame before this block
    analyzer._process_rds(iq)
    assert analyzer._rds_rx is not rx
    assert analyzer._rds_rx.parser is rx.parser  # station accumulator kept


def test_analyzer_rds_pick_visits_every_channel_despite_resorted_targets():
    # Survey re-sorts RDS targets by SNR every pass; a rotating index over
    # that list skipped some stations for whole rounds (live 2026-09-26).
    analyzer, _results = _rds_analyzer([])
    a, b, c = _RDS_STATION_HZ - 200_000, _RDS_STATION_HZ, _RDS_STATION_HZ + 200_000
    orders = [[a, b, c], [b, a, c], [a, c, b], [c, a, b], [b, c, a], [a, b, c]]
    picked = [analyzer._rds_pick(order) for order in orders]
    assert sorted(picked[:3]) == [a, b, c]
    assert sorted(picked[3:]) == [a, b, c]


def test_analyzer_rds_pick_slows_down_channels_without_rds():
    analyzer, _results = _rds_analyzer([])
    from msdr.survey.rds import RdsParser

    live, dead = _RDS_STATION_HZ, _RDS_STATION_HZ + 100_000  # dead: a side peak
    analyzer._rds_stations[live] = RdsParser()
    analyzer._rds_stations[live].groups = 5
    picked = [analyzer._rds_pick([live, dead]) for _ in range(12)]
    # Both get their first dwells; then the silent one only every few rounds.
    assert picked[:4] == [live, dead, live, dead]
    assert picked[4:].count(dead) <= 2


def test_analyzer_rds_dwell_survives_target_flicker():
    analyzer, _results = _rds_analyzer([])
    other = _RDS_STATION_HZ + 400_000
    iq = np.zeros(100_000, dtype=np.complex64)
    analyzer.set_rds_targets([_RDS_STATION_HZ])
    analyzer._process_rds(iq)
    rx = analyzer._rds_rx
    # The station's label drops out for a pass: the dwell keeps going.
    analyzer.set_rds_targets([other])
    analyzer._process_rds(iq)
    assert analyzer._rds_current == _RDS_STATION_HZ
    assert analyzer._rds_rx is rx


async def test_analyzer_measures_delivered_iq_rate():
    # 20 frames of 100k samples, 0.02 s apart: 5 MS/s delivered, whatever the
    # span's nominal rate says (SDRconnect can fall short under load).
    frames = [_iq_to_frame_bytes(np.zeros(100_000, dtype=np.complex64))] * 200
    analyzer, _results = _rds_analyzer(frames, want_rds=False)
    await analyzer.start()
    await asyncio.sleep(2.6)
    rate = analyzer.status()["iq"]["rate_hz"]
    await analyzer.stop()
    assert rate is not None and 3e6 < rate < 6e6


async def test_analyzer_analyzes_a_signal_that_never_goes_quiet():
    # Live 2026-09-26: a steady 24 dB FM carrier stayed "waiting for signal"
    # forever -- its own always-on block power became its noise floor. The
    # band floor (the other, empty channels) must let it through.
    target = 25_000
    rng = np.random.default_rng(3)
    total_raw = 300_000  # 1.5 s, carrier on throughout
    t = np.arange(total_raw) / _SAMPLE_RATE_HZ
    noise = (rng.normal(size=total_raw) + 1j * rng.normal(size=total_raw)) * 0.003
    raw = (0.3 * np.exp(1j * 2 * np.pi * target * t) + noise).astype(np.complex64)
    chunk = 2048
    frames = [_iq_to_frame_bytes(raw[i : i + chunk]) for i in range(0, total_raw, chunk)]
    analyzer, results, _statuses = _make_analyzer(
        lambda: FakeIQStream(frames, chunk / _SAMPLE_RATE_HZ), dwell_s=0.2, min_active_s=0.3
    )
    analyzer.set_targets([target])
    await analyzer.start()
    await asyncio.sleep(1.2)
    await analyzer.stop()
    assert any(fields.get("analyzed_at") is not None for _, fields in results)


def test_analyzer_target_survives_centroid_jitter():
    # Live 2026-09-26: a bursty label's centroid moved a few hundred Hz each
    # frame; exact matching reset the target (and its accumulator) every time.
    analyzer, _results = _rds_analyzer([])
    analyzer.set_targets([144_393_100, 147_060_000])
    from msdr.survey.analyzer import _TargetState

    state = analyzer._target_state.setdefault(144_393_100, _TargetState())
    state.attempts = 3
    analyzer.set_targets([144_392_400, 147_060_300])
    assert analyzer._targets == [144_393_100, 147_060_000]
    assert analyzer._target_state[144_393_100].attempts == 3
    for f in list(analyzer._target_seen):
        analyzer._target_seen[f] -= 60.0  # the old ones left long ago
    analyzer.set_targets([144_420_000])  # 27 kHz away: a different signal
    assert analyzer._targets == [144_420_000]


def test_narrow_channel_puts_the_target_at_dc_with_full_power():
    from msdr.survey.analyzer import _NarrowChannel

    fs = 10_000_000
    offset = 1_513_672.0  # off the channelizer's 12.5 kHz grid on purpose
    n = 1_000_000
    t = np.arange(n) / fs
    x = (0.3 * np.exp(2j * np.pi * (offset + 500.0) * t)).astype(np.complex64)
    ch = _NarrowChannel(fs, offset)
    y = np.concatenate([ch.process(x[i : i + 100_000]) for i in range(0, n, 100_000)])
    assert ch.out_rate == 50_000.0
    y = y[1000:]  # past the filter start-up
    power = float(np.mean(np.abs(y) ** 2))
    assert abs(10 * np.log10(power / 0.09)) < 0.5  # unity gain in the passband
    spec = np.abs(np.fft.fftshift(np.fft.fft(y)))
    peak_hz = (np.argmax(spec) - y.size / 2) * ch.out_rate / y.size
    assert abs(peak_hz - 500.0) < 20.0  # mixed exactly: the tone stays at +500 Hz


def test_analyzer_dwell_survives_target_flickering_out_of_the_top_n():
    # Live 2026-09-26: 7 labels for 6 analyzer slots -- the current target
    # dropped out of the re-ranked list for a frame now and then, and every
    # time its dwell restarted: all targets stuck at 0 attempts.
    analyzer, _results = _rds_analyzer([])
    a, b = 144_390_000, 147_060_000
    analyzer.set_targets([a, b])
    assert analyzer._current_freq == a
    analyzer._accum = [np.zeros(1000, dtype=np.complex64)]
    analyzer._accum_len = 1000
    analyzer.set_targets([b])  # a missed the cut for one frame
    assert analyzer._current_freq == a
    assert analyzer._accum_len == 1000  # dwell kept
    analyzer.set_targets([a + 300, b])  # back, jittered
    assert analyzer._current_freq == a
    assert analyzer._accum_len == 1000


async def test_pump_survives_a_processing_error_and_reports_it():
    # Review 2026-09-26: an exception in _process_block killed the pump for
    # the session while last_error stayed empty.
    frames = [_iq_to_frame_bytes(np.zeros(4096, dtype=np.complex64))] * 6
    analyzer, _results = _rds_analyzer(frames, want_rds=False)
    calls = {"n": 0}
    real = analyzer._process_block

    def flaky(iq):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AssertionError("state torn down mid-block")
        return real(iq)

    analyzer._process_block = flaky
    await analyzer.start()
    await asyncio.sleep(0.4)
    status = analyzer.status()
    await analyzer.stop()
    assert status["last_error"].startswith("AssertionError")
    assert calls["n"] >= 3  # kept going after the failure
    assert not analyzer._pump_task or analyzer._pump_task.cancelled() or analyzer._pump_task.done()


async def test_retune_is_serialised_with_block_processing():
    analyzer, _results = _rds_analyzer([], want_rds=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_block(iq):
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        return None

    analyzer._process_block = slow_block
    analyzer._queue.put_nowait(types.SimpleNamespace(data=b"\0" * 16))
    analyzer._pump_task = asyncio.create_task(analyzer._pump())
    await entered.wait()
    retune = asyncio.create_task(analyzer.retune(100_000_000, 2_000_000))
    await asyncio.sleep(0.05)
    assert not retune.done()  # waits for the block in flight
    release.set()
    await asyncio.wait_for(retune, 2.0)
    assert analyzer._center_hz == 100_000_000
    analyzer._pump_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await analyzer._pump_task


# -- S4: raw/snapped rates, GMSK 9600 family, voice wording ------------------

_NARROW_RATE = 50_000.0  # the analyzer's per-target channel rate


def _lowpass_c(x: np.ndarray, cutoff_hz: float, rate: float, taps: int = 63) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / rate * n) * np.hamming(taps)
    return np.convolve(x, h / h.sum(), mode="same")


def _gmsk_bursts(snr_db: float, seed: int) -> np.ndarray:
    from tests.helpers import gmsk

    rng = np.random.default_rng(seed)
    x = np.concatenate(
        [
            gmsk.rotate(
                gmsk.make_burst(gmsk.random_payload(rng), _NARROW_RATE, rng),
                300.0,
                rng.uniform(0, 2 * np.pi),
                _NARROW_RATE,
            )
            for _ in range(40)
        ]
    )
    noise = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    x = x + noise * np.sqrt(10 ** (-snr_db / 10) / 2)
    # the narrow channel's +-10 kHz filter
    return _lowpass_c(x, 10_000.0, _NARROW_RATE).astype(np.complex64)


def test_gmsk_9600_bursts_are_fm_fsk_with_raw_snapped_rates_and_9600_family():
    analyzer, _results, _statuses = _make_analyzer(lambda: FakeIQStream([], 0.01))
    for snr_db, seed in ((10.0, 31), (30.0, 32)):
        x = _gmsk_bursts(snr_db, seed)
        _hz, fields = analyzer._analyze_active_signal(12500, x, _NARROW_RATE, 1.0, 1.0)
        assert fields["modulation"]["kind"] == "fm_fsk", snr_db
        assert fields["symbol_rate_hz"] == 9600.0
        assert fields["symbol_rate_snapped_hz"] == 9600.0
        assert abs(fields["symbol_rate_raw_hz"] - 9600.0) / 9600.0 <= 0.03
        assert fields["symbol_rate_tolerance"] == 0.03
        assert fields["symbol_rate_method"] == "ifreq_crossings"
        family = fields["family"]
        assert family["name"] == "GMSK/FSK 9600 (AIS or packet)"
        assert family["suggested_decoders"] == ["ais", "packet9600"]
        assert family["alternatives"] == ["AX.25 9600 (G3RUH)", "P25 C4FM (4800 sym/s ×2)"]
        assert "CRC-valid frame with an MMSI" in family["next_action"]


def test_waiting_for_signal_fields_carry_rate_keys():
    analyzer, _results, _statuses = _make_analyzer(lambda: FakeIQStream([], 0.01))
    analyzer.set_targets([12500])
    rng = np.random.default_rng(33)
    noise = (rng.normal(0, 1e-4, 1000) + 1j * rng.normal(0, 1e-4, 1000)).astype(np.complex64)
    _hz, fields = analyzer._analyze_target(noise, analyzer._channelizer.out_rate)
    assert fields["note"] == "waiting for signal"
    assert fields["symbol_rate_hz"] is None
    assert fields["symbol_rate_raw_hz"] is None
    assert fields["symbol_rate_snapped_hz"] is None
    assert fields["symbol_rate_tolerance"] == 0.03


def _voice_fields(monkeypatch, modulation_kind: str) -> dict:
    import msdr.survey.analyzer as analyzer_mod

    monkeypatch.setattr(
        analyzer_mod, "classify_modulation", lambda f: {"kind": modulation_kind, "confidence": 0.8}
    )
    monkeypatch.setattr(
        analyzer_mod,
        "voice_score",
        lambda audio, rate: {"kind": "voice", "confidence": 0.9, "score": 0.9},
    )
    analyzer, _results, _statuses = _make_analyzer(
        lambda: FakeIQStream([], 0.01), want_voice=lambda: True
    )
    t = np.arange(int(_NARROW_RATE)) / _NARROW_RATE
    # A steady tone: ifreq spread far below the FM-voice promotion limit.
    x = np.exp(1j * 2 * np.pi * 200.0 * t).astype(np.complex64)
    _hz, fields = analyzer._analyze_active_signal(12500, x, _NARROW_RATE, 1.0, 1.0)
    return fields


def test_nfm_voice_label_only_when_modulation_is_fm_like(monkeypatch):
    fields = _voice_fields(monkeypatch, "fm_fsk")
    assert fields["family"]["name"] == "speech-like NFM audio (tentative)"
    assert fields["symbol_rate_hz"] is None
    assert fields["symbol_rate_raw_hz"] is None


def test_speech_like_audio_without_fm_evidence_is_not_called_nfm_voice(monkeypatch):
    fields = _voice_fields(monkeypatch, "unknown")
    assert fields["modulation"]["kind"] == "unknown"
    family = fields["family"]
    assert family["name"] == "speech-like audio (demodulation untested)"
    assert family["next_action"] == "listen / tested path: NFM discriminator"
    assert family["suggested_decoders"] == []


def test_gate_records_interior_on_run_durations_for_keying():
    from msdr.survey.analyzer import _TargetState

    analyzer, _results, _statuses = _make_analyzer(lambda: FakeIQStream([], 0.01))
    rate = 25_000.0
    rng = np.random.default_rng(34)

    def quiet(s):
        n = int(rate * s)
        return (rng.normal(0, 1e-4, n) + 1j * rng.normal(0, 1e-4, n)).astype(np.complex64)

    def on(s):
        return (0.5 * np.ones(int(rate * s))).astype(np.complex64)

    buf = np.concatenate([quiet(0.3), on(0.06), quiet(0.06), on(0.18), quiet(0.06), on(0.06)])
    buf = np.concatenate([buf, quiet(0.3)])
    state = _TargetState()
    analyzer._gate_active_blocks(buf, rate, state)
    assert [round(r, 2) for r in state.on_runs] == [0.06, 0.18, 0.06]


def test_digital_symbol_evidence_survives_false_positive_speech_score(monkeypatch):
    import msdr.survey.analyzer as analyzer_mod

    monkeypatch.setattr(
        analyzer_mod,
        "voice_score",
        lambda audio, rate: {"kind": "voice", "confidence": 0.95, "score": 1.0},
    )
    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0.01), want_voice=lambda: True)
    for snr_db, seed in ((10.0, 31), (30.0, 32)):
        _, fields = analyzer._analyze_active_signal(
            12500, _gmsk_bursts(snr_db, seed), _NARROW_RATE, 1.0, 1.0
        )
        assert fields["voice"]["kind"] == "data"
        assert fields["symbol_rate_hz"] == 9600.0
        assert fields["family"]["name"] == "GMSK/FSK 9600 (AIS or packet)"


def test_four_level_fsk_clock_survives_false_speech_score(monkeypatch):
    import msdr.survey.analyzer as analyzer_mod

    rate = _NARROW_RATE
    n = int(rate)
    rng = np.random.default_rng(8)
    symbol_index = np.floor(np.arange(n) * 4800.0 / rate).astype(int)
    levels = rng.choice([-3.0, -1.0, 1.0, 3.0], size=symbol_index[-1] + 1)
    frequency = levels[symbol_index] * 650.0
    taps = np.exp(-0.5 * (np.arange(-12, 13) / 3.0) ** 2)
    frequency = np.convolve(frequency, taps / taps.sum(), mode="same")
    iq = np.exp(1j * 2 * np.pi * np.cumsum(frequency) / rate).astype(np.complex64)

    monkeypatch.setattr(
        analyzer_mod,
        "voice_score",
        lambda audio, rate: {"kind": "voice", "confidence": 0.95, "score": 1.0},
    )
    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0.01), want_voice=lambda: True)
    _, fields = analyzer._analyze_active_signal(12500, iq, rate, 1.0, 1.0)
    assert fields["voice"]["kind"] == "data"
    assert fields["symbol_rate_hz"] == 4800.0
    assert fields["symbol_rate_method"] == "fsk_clock_line"
    assert fields["family"]["name"] == "DMR / P25 / NXDN"


def test_noaa_like_analog_speech_keeps_voice_classification():
    rate = _NARROW_RATE
    n = int(rate)
    t = np.arange(n) / rate
    rng = np.random.default_rng(3)
    spec = np.fft.rfft(rng.normal(size=n))
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    spec[(freqs < 300.0) | (freqs > 3000.0)] = 0.0
    speech = np.fft.irfft(spec, n)
    speech /= np.max(np.abs(speech))
    speech *= 1.0 + 0.8 * np.cos(2 * np.pi * 4.0 * t)
    speech = 0.65 * speech / np.max(np.abs(speech))
    speech += 0.07 * np.sin(2 * np.pi * 1050.0 * t)
    iq = np.exp(1j * 2 * np.pi * np.cumsum(5000.0 * speech) / rate).astype(np.complex64)

    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0.01), want_voice=lambda: True)
    _, fields = analyzer._analyze_active_signal(12500, iq, rate, 1.0, 1.0)
    assert fields["voice"]["kind"] == "voice"
    assert fields["symbol_rate_hz"] is None
    assert fields["family"]["name"] == "speech-like NFM audio (tentative)"


async def test_slow_detector_result_is_retained_and_flagged_stale():
    # Review 2026-09-28: max_job_age_s is the queue deadline; expiring the
    # displayed result on capture age made every slow inference vanish at
    # once. The result now lives for detector.result_ttl_s with its capture
    # age reported and a stale flag past max_job_age_s.
    settings = RFMLSettings(enabled=False)
    settings.detector.enabled = True
    settings.detector.max_job_age_s = 5.0
    settings.detector.result_ttl_s = 30.0
    now = [1000.0]
    analyzer = Analyzer(
        iq_stream_factory=lambda: FakeIQStream([], 0),
        center_hz=0,
        sample_rate_hz=_SAMPLE_RATE_HZ,
        max_analyzed=1,
        dwell_s=0.05,
        min_snr_db=8.0,
        on_result=lambda _freq, _fields: None,
        on_status=lambda: None,
        ml_settings=settings,
    )
    analyzer._clock = lambda: now[0]
    # Captured 6 s ago, published now: slower than the queue deadline.
    analyzer._ml_detector.on_result(
        {"analyzer_epoch": analyzer._retune_gen, "capture_monotonic": now[0] - 6.0},
        {"candidates": [{"low_hz": 1.0, "high_hz": 2.0, "score": 0.9}]},
    )
    status = analyzer.detector_result()
    assert status["result"] is not None
    assert abs(status["result_age_s"] - 6.0) < 1e-6
    assert status["result_stale"] is True

    now[0] += 30.0  # past result_ttl_s: gone, age still reported
    status = analyzer.detector_result()
    assert status["result"] is None
    assert abs(status["result_age_s"] - 36.0) < 1e-6


async def test_validation_capture_without_ml_and_retune_guard():
    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0))
    analyzer._started = True
    pending = asyncio.create_task(analyzer.capture_validation("wideband"))
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="already pending"):
        await analyzer.capture_validation("wideband")
    iq = np.ones(4096, dtype=np.complex64)
    analyzer._capture_validation_window("wideband", iq, 50000, 144390000, {})
    samples, rate, metadata = await pending
    assert rate == 50000 and metadata["frequency_hz"] == 144390000
    assert samples is not iq and samples.size == 4096
    pending = asyncio.create_task(analyzer.capture_validation("wideband"))
    await asyncio.sleep(0)
    analyzer._retune_gen += 1
    analyzer._capture_validation_window("wideband", iq, 50000, 144390000, {})
    with pytest.raises(ValueError, match="changed"):
        await pending


@pytest.mark.parametrize("duration", [1, 3, 5])
async def test_timed_validation_capture_is_contiguous_and_exact(duration):
    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0))
    analyzer._started = True
    pending = asyncio.create_task(analyzer.capture_validation("wideband", duration))
    await asyncio.sleep(0)
    rate = 10000
    for index in range(0, duration * rate, 4096):
        analyzer._capture_validation_window(
            "wideband", np.full(4096, index, dtype=np.complex64), rate, 160815000,
            {"sample_index": index, "run_index": 0, "source_generation": 1},
        )
    samples, saved_rate, metadata = await pending
    assert len(samples) == duration * rate
    assert saved_rate == rate and metadata["sample_index"] == 0
    assert samples[4096] == 4096
    assert metadata["requested_duration_s"] == duration


async def test_timed_validation_capture_rejects_gaps():
    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0))
    analyzer._started = True
    pending = asyncio.create_task(analyzer.capture_validation("wideband", 3))
    await asyncio.sleep(0)
    for index in (0, 8192):
        analyzer._capture_validation_window(
            "wideband", np.ones(4096, dtype=np.complex64), 10000, 160815000,
            {"sample_index": index},
        )
    with pytest.raises(ValueError, match="gap"):
        await pending
    assert analyzer._validation_capture is None


def test_narrow_morse_timing_overrides_noisy_fm_but_not_wide_signal(monkeypatch):
    import msdr.survey.analyzer as module

    analyzer, _, _ = _make_analyzer(lambda: FakeIQStream([], 0.01), want_voice=lambda: True)
    monkeypatch.setattr(
        module, "classify_modulation", lambda f: {"kind": "fm_fsk", "confidence": 0.85}
    )
    monkeypatch.setattr(module, "occupied_bw_hz", lambda *a: 300.0)
    samples = np.ones(25000, dtype=np.complex64)
    runs = [0.06, 0.18] * 5
    _, fields = analyzer._analyze_active_signal(
        432304000, samples, 25000, 0.5, 1, env_cv=0.1, gaps=10, on_runs=runs
    )
    assert fields["family"]["suggested_decoders"] == ["cw"]
    assert fields["voice"] is None
    monkeypatch.setattr(module, "occupied_bw_hz", lambda *a: 8000.0)
    _, fields = analyzer._analyze_active_signal(
        432304000, samples, 25000, 0.5, 1, env_cv=0.1, gaps=10, on_runs=runs
    )
    assert fields["modulation"]["kind"] != "ook"


async def test_stop_tears_down_stream_even_if_ml_stop_raises():
    class _BadML(_MLStub):
        async def stop(self) -> None:
            raise RuntimeError("ml stop failed")

    streams = []

    def factory():
        streams.append(FakeIQStream([], 0.01))
        return streams[-1]

    analyzer, _results, _statuses = _make_analyzer(factory)
    analyzer.set_targets([12500])
    analyzer._ml = _BadML()
    await analyzer.start()
    with pytest.raises(RuntimeError, match="ml stop failed"):
        await analyzer.stop()
    assert analyzer._reader_task is None and analyzer._pump_task is None
    assert streams[0].started is False  # IQ stream was still stopped
