"""Analyzer: IQ modulation/symbol-rate/family/voice classifier.

Same IQ pump pattern as `msdr/decoder/band.py`'s BandDecoder -- own IQStream
connection, `PolyphaseChannelizer`, a bounded drop-oldest frame queue, numpy
work off the event loop via `asyncio.to_thread`, an EMA'd cpu_load and a
"connected" rule keyed on how long ago a frame last arrived -- but where
BandDecoder keeps one NFMChannel+backend per detected channel, this instead
round-robins one channel at a time across up to `max_analyzed` spectrum-tracker targets,
accumulates `dwell_s` worth of that channel's output, and turns it into
`features.py`/`families.py` results instead of decoder events.
"""

import asyncio
import contextlib
import functools
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from msdr.decoder.backends import reap_cancelled
from msdr.decoder.channelizer import PolyphaseChannelizer
from msdr.survey.cw import cw_evidence
from msdr.survey.families import family_for
from msdr.survey.features import (
    BIMODAL_GATE,
    bell202_score,
    classify_modulation,
    crossing_rate_hz,
    envelope_cv,
    fsk_clock_line,
    ifreq_bimodality,
    ifreq_std_hz,
    keying_timing,
    occupied_bw_hz,
    snap_symbol_rate,
    spectral_symmetry,
    symbol_rate_hz,
    voice_score,
)
from msdr.survey.rds import RdsParser, RdsReceiver, _DecimFIR, _lowpass, fm_channel_hz

# Backpressure, same as BandDecoder: more IQ frames queued than this and the
# oldest is dropped rather than blocking the reader.
_MAX_QUEUED_FRAMES = 10
# "connected" = a frame arrived within this many seconds (IQStream has no
# connected attribute of its own -- same rule as BandDecoder).
_IQ_CONNECTED_TIMEOUT_S = 2.0
# NBFM peak deviation, matching channelizer.py's NFMChannel: normalises the
# discriminator's Hz output to roughly +-1 for voice_score's dBFS silence
# check to mean the same thing it does for real demodulated audio.
_MAX_DEVIATION_HZ = 5000.0
# A line has to explain at least this share of the clock search's power to
# count as "this looks like a real symbol clock" for classify_modulation's
# psk_like rule (no FSK line) -- below it, it's noise-floor wobble.
_FSK_LINE_GATE = 0.15

# Activity gate (live-signal fix): most spectrum-tracker targets are bursty/
# intermittent (duty 0.0-0.3 in the field), so a fixed dwell mostly captures
# silence -- feeding that straight to the feature extractors is what produced
# the degenerate "unknown, 16666.67 Hz" noise-artifact readings. Instead each
# dwell is split into 20 ms blocks, block power compared against a per-target
# noise floor, and only blocks that clear the floor by a margin are kept.
# 5 ms: short data bursts (AIS: 26 ms) must fill whole blocks -- at 20 ms a
# burst was one or two partly-empty blocks, all edge.
_ACTIVITY_BLOCK_S = 0.005
_ACTIVITY_MARGIN_DB = 6.0
_NOISE_FLOOR_PERCENTILE = 10.0
# EMA weight for this dwell's floor estimate -- slow enough that one
# unusually signal-heavy dwell (e.g. a long burst) doesn't drag the floor up
# on its own, since most dwells on a bursty target are mostly quiet.
_NOISE_FLOOR_EMA_ALPHA = 0.2
# Band noise floor: this percentile of all channelizer channels' power (most
# of a wide span is empty), EMA-smoothed per IQ block. A target's own floor
# can't see a signal that is on for the whole dwell -- a steady carrier or a
# long QSO made its own 10th-percentile block power the "floor", so nothing
# ever cleared it (live 2026-09-26: 147.074 MHz FM voice, 24 dB SNR,
# active_s 0.0 after 10 dwells). The gate uses the lower of the two floors.
_BAND_FLOOR_PERCENTILE = 30.0
_BAND_FLOOR_EMA_ALPHA = 0.2
# Per-target active-sample accumulator: capped at this many seconds of
# samples (deque, drop-oldest) and this much wall-clock age.
_ACCUM_MAX_S = 3.0
_ACCUM_MAX_AGE_S = 120.0
_DEFAULT_MIN_ACTIVE_S = 0.6
# voice_score needs at least this much active audio to mean anything.
_MIN_VOICE_ACTIVE_S = 0.5
# features.bell202_score at/above this = Bell 202 AFSK (noise/voice ~1-2).
_BELL202_MIN_SCORE = 4.0
# set_targets: a label frequency this close to a known target is that target
# (centroid jitter), not a new one -- under half an NFM channel spacing.
_TARGET_MATCH_HZ = 6_000
# ...and one missing from the list keeps its state (and a running dwell)
# for this long -- top-N re-ranking makes targets at the cut-off flicker.
_TARGET_GRACE_S = 30.0
# Re-centre a target's channel when the analysis finds it this far off.
_RETUNE_HZ = 2_000
# A "carrier" keyed off and on this many times inside dwells is OOK/CW.
_OOK_MIN_GAPS = 3
_VOICE_BAND_HZ = 3_400.0
# _NarrowChannel's noise power over the band floor (a channelizer slot's):
# measured live 2026-09-26, -69.6 vs -71.7 dB on an empty channel.
_NARROW_NOISE_EXCESS_DB = 2.0
# Voice with at least this instantaneous-frequency spread is FM voice.
_FM_VOICE_MIN_IFREQ_STD_HZ = 1_000.0
# snap_symbol_rate's relative window, reported with the raw/snapped rates.
_SYMBOL_RATE_TOLERANCE = 0.03
# Interior on-run durations kept per target for keying_timing (CW check).
_MAX_ON_RUNS = 64


@functools.lru_cache(maxsize=4)
def _VOICE_LOWPASS(rate: float) -> np.ndarray:  # noqa: N802 -- constant-like
    return _lowpass(201, _VOICE_BAND_HZ, rate)


# RDS dwells (wideband FM broadcast labels): one station at a time, running
# alongside the narrowband round-robin on the same raw IQ blocks. Paused
# while cpu_load is above the high mark (resumed below the low one, so the
# RDS chain's own cost doesn't make it flap on and off every block).
_RDS_CPU_PAUSE = 0.7
_RDS_CPU_RESUME = 0.6
# Stations are tuned to, keyed by and reported at their FM channel centre
# (rds.fm_channel_hz, 100 kHz grid): a label centroid is only ~15 kHz
# precise, and a side peak can sit 40+ kHz off -- an unsnapped key once split
# one station's partial PS over two accumulators that never completed.
# Stay this far inside the span edge so the ~+-120 kHz channel filter fits.
_RDS_EDGE_MARGIN_HZ = 150_000
_IQ_RATE_WINDOW_S = 2.0
# A channel with no RDS group after this many dwells (a side peak, an HD
# sideband, a station without RDS) is revisited at 1/_RDS_DEAD_SLOWDOWN the
# rate of the others, so the real stations get most of the round.
_RDS_DEAD_DWELLS = 2
_RDS_DEAD_SLOWDOWN = 4


class _TargetState:
    """Per-target activity-gate state, keyed by frequency in Analyzer._target_state
    and persisted across round-robin visits (only dropped when the frequency
    itself leaves the target list -- see set_targets/retune)."""

    __slots__ = (
        "noise_floor_db",
        "chunks",
        "active_samples",
        "attempts",
        "analyzed",
        "gaps",
        "on_runs",
        "tune_hz",
    )

    def __init__(self) -> None:
        self.noise_floor_db: float | None = None
        # (timestamp, one active run normalised to unit power, its denoised
        # envelope cv^2) -- see Analyzer._gate_active_blocks
        self.chunks: deque[tuple[float, np.ndarray, float]] = deque()
        self.active_samples = 0
        self.attempts = 0
        self.analyzed = False
        # quiet gaps seen *inside* dwells (keying), not dwell boundaries
        self.gaps = 0
        # durations (s) of on-runs bounded by quiet on both sides inside a
        # dwell -- the keying's element lengths, for keying_timing()
        self.on_runs: deque[float] = deque(maxlen=_MAX_ON_RUNS)
        # measured centre to mix at, once an analysis found the label off
        self.tune_hz: float | None = None


# Target channel (see _NarrowChannel): complex baseband at ~this rate, flat
# to +-_NARROW_CUTOFF_HZ around the target -- an NFM signal's Carson
# bandwidth (+-5 kHz deviation + 3 kHz audio ~= +-8 kHz) fits with margin.
# 50 kHz: 9600 baud (AIS, packet 9k6) is 5 samples/symbol -- at 25 kHz its
# 2.6 samples sat inside the filter's own correlation and couldn't be told
# from noise.
_NARROW_RATE_HZ = 50_000.0
_NARROW_CUTOFF_HZ = 10_000.0


class _NarrowChannel:
    """The current target, mixed down exactly and low-passed from the raw
    IQ -- not the channelizer's nearest slot: its 12.5 kHz grid is anchored on
    the device centre (not on the band's channel raster), so a target sits up
    to +-6.25 kHz off a slot centre, and a slot passes only +-6.25 kHz -- half
    of an off-centre NFM signal was cut away, which read as a big envelope
    swing and garbage features (live 2026-09-26: APRS 144.390, 5 kHz off its
    slot, env_cv 0.54, "unknown"). Same band-pass-then-derotate trick as
    rds.WfmChannel; two stages when the decimation splits as n x 20."""

    def __init__(self, fs_in: float, offset_hz: float) -> None:
        total = max(1, int(round(fs_in / _NARROW_RATE_HZ)))
        d2 = 20 if total > 20 and total % 20 == 0 else 1
        d1 = total // d2
        r1 = fs_in / d1
        if d2 > 1:
            # Wide transition: only what aliases into the final +-12.5 kHz
            # after stage 2 matters.
            h1 = _lowpass(8 * d1, min(0.2 * r1, r1 / 2 - 2 * _NARROW_CUTOFF_HZ), fs_in)
        else:
            h1 = _lowpass(16 * d1, _NARROW_CUTOFF_HZ, fs_in)
        w0 = 2 * np.pi * offset_hz / fs_in
        self._stage1 = _DecimFIR(h1 * np.exp(1j * w0 * np.arange(h1.size)), d1)
        self._derot_step = (w0 * d1) % (2 * np.pi)
        self._derot_phase = (w0 * (d1 - 1)) % (2 * np.pi)
        self._stage2 = _DecimFIR(_lowpass(24 * d2, _NARROW_CUTOFF_HZ, r1), d2) if d2 > 1 else None
        self.out_rate = fs_in / total

    def process(self, iq: np.ndarray) -> np.ndarray:
        y = self._stage1.process(iq)
        if y.size == 0:
            return y
        ph = self._derot_phase + self._derot_step * np.arange(y.size)
        y = y * np.exp(-1j * ph).astype(np.complex64)
        self._derot_phase = float((self._derot_phase + self._derot_step * y.size) % (2 * np.pi))
        if self._stage2 is not None:
            y = self._stage2.process(y)
        return y


def _block_power_db(block: np.ndarray) -> float:
    power = float(np.mean(np.abs(block) ** 2))
    return 10.0 * np.log10(power + 1e-20)


def _frame_to_iq(data: bytes) -> np.ndarray:
    """int16 interleaved I/Q (IQStream's format) -> complex64 in [-1, 1].
    Same conversion as band.py's `_frame_to_iq` -- duplicated rather than
    imported since that one is band.py-private and this module owns no
    dependency on decoder internals beyond the channelizer itself."""
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    usable = (samples.size // 2) * 2
    samples = samples[:usable]
    return (samples[0::2] + 1j * samples[1::2]).astype(np.complex64)


def _psd_centroid_hz(x: np.ndarray, rate: float) -> float:
    """Power-weighted mean frequency: the fine correction the plan calls
    for, on top of the channelizer's own fine-mix -- the spectrum tracker's target frequency
    comes from a coarse 512-bin spectrum (up to 15.6 kHz/bin at 8 MS/s), so
    the emitter can sit anywhere within that bin, not exactly on it."""
    n = x.size
    if n < 8:
        return 0.0
    spec = np.fft.fftshift(np.fft.fft(x * np.hanning(n)))
    psd = np.abs(spec) ** 2
    total = float(psd.sum())
    if total <= 0.0:
        return 0.0
    freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / rate))
    return float(np.sum(freqs * psd) / total)


def _fine_mix(x: np.ndarray, offset_hz: float, rate: float) -> np.ndarray:
    """Shift `x` down by `offset_hz` -- a one-shot bulk mix over the whole
    accumulated dwell buffer (not a per-frame NCO carried across calls like
    NFMChannel's, since by the time this runs the buffer is already
    complete and phase continuity across calls no longer matters)."""
    if x.size == 0 or offset_hz == 0.0:
        return x
    n = np.arange(x.size, dtype=np.float64)
    mixer = np.exp(-1j * 2 * np.pi * offset_hz * n / rate).astype(np.complex64)
    return x * mixer


def _discriminator_audio(x: np.ndarray, rate: float) -> np.ndarray:
    """Standard complex discriminator (same maths as NFMChannel.process),
    normalised so +-`_MAX_DEVIATION_HZ` maps to roughly +-1 -- voice_score's
    dBFS silence check assumes that scale."""
    if x.size < 2:
        return np.zeros(0, dtype=np.float32)
    prod = x[1:] * np.conj(x[:-1])
    audio = np.angle(prod).astype(np.float32)
    audio *= np.float32(rate / (2 * np.pi * _MAX_DEVIATION_HZ))
    return audio


class Analyzer:
    def __init__(
        self,
        *,
        iq_stream_factory: Callable[[], object],
        center_hz: int,
        sample_rate_hz: int,
        max_analyzed: int,
        dwell_s: float,
        min_snr_db: float,
        on_result: Callable[[int, dict], None],
        on_status: Callable[[], None],
        spacing_hz: int = 12500,
        want_voice: Callable[[], bool] | None = None,
        min_active_s: float = _DEFAULT_MIN_ACTIVE_S,
        want_rds: Callable[[], bool] | None = None,
        rds_dwell_s: float = 6.0,
    ) -> None:
        self._iq_stream_factory = iq_stream_factory
        # Replay's bound iq_stream method exposes its source. Subscribe to the
        # generation clock so seeks invalidate queued and in-flight analysis.
        # Live stream factories have no IQSource owner and keep existing behavior.
        self._source = getattr(iq_stream_factory, "__self__", None)
        if not (
            self._source is not None
            and callable(getattr(self._source, "descriptor", None))
            and callable(getattr(self._source, "add_listener", None))
            and callable(getattr(self._source, "remove_listener", None))
        ):
            self._source = None
        desc = self._source.descriptor() if self._source is not None else None
        self._source_generation = desc.generation if desc is not None else None
        self._processed_source_generation = self._source_generation
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._max_analyzed = max_analyzed
        self._dwell_s = dwell_s
        self._min_active_s = min_active_s
        # Not used inside the loop -- Survey already filters targets by SNR
        # before calling set_targets (only Survey's labels carry an SNR).
        # Kept so status()/constructor mirror the plan's contract.
        self._min_snr_db = min_snr_db
        self._on_result = on_result
        self._on_status = on_status
        self._spacing_hz = spacing_hz
        self._want_voice = want_voice if want_voice is not None else (lambda: False)

        self._channelizer = PolyphaseChannelizer(int(sample_rate_hz), spacing_hz)

        self._targets: list[int] = []
        # Survey supplies the eligible pool; only _targets is actively
        # channelized. The extra candidates rotate in after complete dwells.
        self._candidate_targets: list[int] = []
        self._priority_freq: int | None = None
        self._rotation_index = 0
        self._single_slot_peer = False
        self._current_index = 0
        self._current_freq: int | None = None
        self._current_k: int | None = None
        self._current_residual_hz = 0.0
        self._accum: list[np.ndarray] = []
        self._accum_len = 0
        # Activity-gate state per target frequency, persisted across
        # round-robin visits -- see set_targets()/retune() for pruning.
        self._target_state: dict[int, _TargetState] = {}

        self._stream = None
        self._started = False
        self._reader_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)

        self._frames = 0
        self._band_floor_db: float | None = None  # see _BAND_FLOOR_PERCENTILE
        self._target_seen: dict[int, float] = {}  # target -> last set_targets() it was in
        self._narrow: _NarrowChannel | None = None  # current target's channel
        self._narrow_key: tuple | None = None
        self._rate_window_start: float | None = None
        self._rate_window_samples = 0
        self._iq_rate_hz: float | None = None
        self._dropped = 0
        self._cpu_load = 0.0
        self._last_frame_at: float | None = None
        self._last_error: str | None = None
        self._clock: Callable[[], float] = time.monotonic
        # retune() rewrites the state _process_block reads from its thread
        # (channelizer, current target, accumulators): the two are serialised
        # on this lock, and a result computed before a retune is dropped.
        self._process_lock = asyncio.Lock()
        self._retune_gen = 0
        self._retune_blocked = False
        self._accept_after_epoch = 0.0
        # RDS: targets refreshed by Survey (set_rds_targets, a plain list
        # swap); all scheduling happens on the worker side in _process_rds.
        self._want_rds = want_rds if want_rds is not None else (lambda: False)
        self._rds_dwell_s = rds_dwell_s
        self._rds_targets: list[int] = []
        self._rds_current: int | None = None
        self._rds_tune_hz: int | None = None
        self._rds_rx: RdsReceiver | None = None
        self._rds_elapsed_s = 0.0
        self._rds_last_ps: str | None = None
        self._rds_paused = False
        self._rds_seen_dropped = 0
        self._rds_block_ms = 0.0
        # Per-station accumulator keyed by tuned frequency, kept across
        # visits (PS needs several group-0A cycles) -- cleared on retune.
        self._rds_stations: dict[int, RdsParser] = {}
        # channel -> [dwell sequence number of its last dwell, dwells so far]
        self._rds_visits: dict[int, list[int]] = {}
        self._rds_seq = 0
        self._rds_pending: list[tuple[int, dict]] = []

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._source is not None:
            desc = self._source.descriptor()
            self._source_generation = desc.generation
            self._processed_source_generation = desc.generation
            self._source.add_listener(self._on_source_event)
        self._stream = self._iq_stream_factory()
        await self._stream.start()
        await self._stream.enable(True)
        self._started = True
        self._reader_task = asyncio.create_task(self._read_frames())
        self._pump_task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        self._started = False
        if self._source is not None:
            self._source.remove_listener(self._on_source_event)
        for task in (self._reader_task, self._pump_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._pump_task):
            if task is not None:
                await reap_cancelled(task)
        self._reader_task = None
        self._pump_task = None
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None
        self._last_frame_at = None
        self._rate_window_start = None
        self._iq_rate_hz = None

    def _invalidate_source_generation(self, generation: int) -> None:
        if self._source_generation is not None and generation <= self._source_generation:
            return
        self._source_generation = generation
        self._retune_gen += 1  # invalidate a worker result already in flight
        while not self._queue.empty():
            self._queue.get_nowait()

    def _on_source_event(self, event) -> None:
        if event.kind == "generation":
            self._invalidate_source_generation(event.generation)

    def begin_retune(self) -> None:
        """Invalidate in-flight work immediately, before waiting for DSP."""
        self._retune_gen += 1
        self._retune_blocked = True
        while not self._queue.empty():
            self._queue.get_nowait()

    async def retune(
        self, center_hz: int, sample_rate_hz: int, *, expected_generation: int | None = None
    ) -> None:
        """Rebuild DSP after in-flight processing stops, then settle live IQ.

        A queued reset from an intermediate ConfirmedState echo cannot reopen
        the gate or overwrite a later span.
        """
        if expected_generation is None:
            if not self._retune_blocked:
                self.begin_retune()
            expected_generation = self._retune_gen
        async with self._process_lock:
            if expected_generation != self._retune_gen:
                return
            self._retune_locked(center_hz, sample_rate_hz)
            self._accept_after_epoch = time.time() + 0.25 if self._source is None else 0.0
            self._retune_blocked = False

    def _retune_locked(self, center_hz: int, sample_rate_hz: int) -> None:
        self._retune_gen += 1
        self._rds_pending = []
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._channelizer = PolyphaseChannelizer(int(sample_rate_hz), self._spacing_hz)
        self._targets = []
        self._candidate_targets = []
        self._priority_freq = None
        self._rotation_index = 0
        self._single_slot_peer = False
        self._current_index = 0
        self._current_freq = None
        self._current_k = None
        self._reset_accum()
        # Every tracked frequency is meaningless in the new span (see the
        # docstring above) -- same as the channelizer's own channel indices.
        self._target_state = {}
        self._target_seen = {}
        self._band_floor_db = None
        self._rds_targets = []
        self._rds_current = None
        self._rds_rx = None
        self._rds_stations = {}
        self._rds_visits = {}
        # Frames already copied from the source queue belong to the old
        # dwell/span and must not run through the reset channelizer.
        while not self._queue.empty():
            self._queue.get_nowait()
        self._on_status()

    # -- targets ------------------------------------------------------------

    def set_targets(
        self, frequencies_hz: list[int], *, priority_frequency_hz: int | None = None
    ) -> None:
        """Refresh candidates without interrupting the current dwell.

        Only max_analyzed targets are active at once. The first is the tuned
        VFO when supplied; the others page through eligible spectrum-tracker candidates
        after completed dwells, never on a spectrum-frame refresh.
        """
        # Survey sends each label's *current* centroid, which jitters by a
        # few hundred Hz from frame to frame on a live (especially bursty)
        # signal. Exact matching treated every jitter as a new target --
        # dwell accumulator reset, activity history dropped, channel rebuilt
        # -- so bursts never accumulated (live 2026-09-26: APRS stayed
        # "unknown"/"voice"). A frequency within _TARGET_MATCH_HZ of a known
        # target keeps that target's identity (and its first estimate, which
        # the analysis refines anyway).
        known = list(self._candidate_targets) + [
            f for f in self._target_state if f not in self._candidate_targets
        ]
        new_candidates: list[int] = []
        priority = (
            priority_frequency_hz
            if priority_frequency_hz is not None and priority_frequency_hz in frequencies_hz
            else None
        )
        if priority is not None:
            new_candidates.append(priority)  # Exact receiver VFO, not a jittering tracker centroid.
        for freq in frequencies_hz:
            if priority is not None and abs(freq - priority) <= _TARGET_MATCH_HZ:
                continue
            match = min(known, key=lambda k: abs(k - freq), default=None)
            key = match if match is not None and abs(match - freq) <= _TARGET_MATCH_HZ else freq
            if key not in new_candidates:
                new_candidates.append(key)
        # SNR/duty rankings change each spectrum frame. Preserve established
        # pool order so the rotation cursor cannot chase moving ranks and
        # repeatedly skip a weak target. Newly appearing tracks join at the
        # tail; the VFO alone always leads.
        established = [
            f for f in self._candidate_targets
            if f in new_candidates and f != priority
        ]
        new_candidates = (
            ([priority] if priority is not None else [])
            + established
            + [f for f in new_candidates if f != priority and f not in established]
        )
        now = self._clock()
        for key in new_candidates:
            self._target_seen[key] = now
        priority_changed = priority != self._priority_freq
        self._priority_freq = priority
        self._candidate_targets = new_candidates
        if priority_changed:
            self._rotation_index = 0
            self._single_slot_peer = False
        new_targets = self._active_page()
        # A target briefly falling out of the spectrum tracker or changing rank must retain its
        # accumulated dwell. A receiver tune is an explicit exception: start
        # analyzing the new VFO immediately.
        current = self._current_freq
        if (
            not priority_changed
            and current is not None
            and current not in new_targets
            and now - self._target_seen.get(current, -1e9) <= _TARGET_GRACE_S
        ):
            if len(new_targets) < max(1, self._max_analyzed):
                new_targets.append(current)
            elif new_targets and new_targets[-1] != priority:
                new_targets[-1] = current
        if current is not None and current in new_targets:
            self._current_index = new_targets.index(current)
        else:
            self._current_index = 0
            self._reset_accum()
        self._targets = new_targets
        self._current_freq = new_targets[self._current_index] if new_targets else None
        self._refresh_current_channel()
        # Keep activity state for eligible candidates across page visits;
        # obsolete tracks still expire after the same grace period.
        for freq, seen_at in list(self._target_seen.items()):
            if freq not in new_candidates and freq != current and now - seen_at > _TARGET_GRACE_S:
                self._target_state.pop(freq, None)
                del self._target_seen[freq]

    def _active_page(self) -> list[int]:
        capacity = max(1, self._max_analyzed)
        priority = self._priority_freq
        pool = [f for f in self._candidate_targets if f != priority]
        if priority is not None and capacity == 1:
            if self._single_slot_peer and pool:
                return [pool[self._rotation_index % len(pool)]]
            return [priority]
        page = [priority] if priority is not None else []
        slots = capacity - len(page)
        if pool and slots:
            offset = self._rotation_index % len(pool)
            page.extend((pool + pool)[offset : offset + min(slots, len(pool))])
        return page

    def set_rds_targets(self, frequencies_hz: list[int]) -> None:
        """WFM labels in the FM broadcast band (Survey filters them). Just a
        list swap -- the worker thread picks it up on its next block."""
        self._rds_targets = list(frequencies_hz)

    def _refresh_current_channel(self) -> None:
        if self._current_freq is None:
            self._current_k = None
            self._current_residual_hz = 0.0
            return
        self._current_k, _ = self._channelizer.channel_for(self._current_freq, self._center_hz)
        # Exactly on the target: no residual left for _analyze_active_signal.
        # Rebuilt only when the target (or span) actually changes -- this runs
        # on every set_targets() call (each spectrum frame), and a fresh
        # filter restarts its history and mixing phase, glitching the dwell.
        state = self._target_state.get(self._current_freq)
        mix_hz = (
            state.tune_hz if state is not None and state.tune_hz is not None else self._current_freq
        )
        key = (mix_hz, self._center_hz, self._sample_rate_hz)
        if self._narrow is None or self._narrow_key != key:
            self._narrow = _NarrowChannel(self._sample_rate_hz, mix_hz - self._center_hz)
            self._narrow_key = key
        self._current_residual_hz = 0.0

    def _reset_accum(self) -> None:
        self._accum = []
        self._accum_len = 0

    def _advance_target(self) -> None:
        self._reset_accum()
        # A large span can have hundreds of eligible peaks. Most are off the
        # active page for minutes; expire their old audio as dwells complete
        # so the fair candidate pool does not retain unbounded IQ memory.
        out_rate = self._sample_rate_hz / max(1, int(round(self._sample_rate_hz / _NARROW_RATE_HZ)))
        now = self._clock()
        for state in self._target_state.values():
            self._trim_target_accum(state, out_rate, now)
        if not self._targets:
            self._current_freq = None
            self._current_k = None
            return
        if self._current_index + 1 < len(self._targets):
            self._current_index += 1
        else:
            priority = self._priority_freq
            pool_size = len(self._candidate_targets) - (priority is not None)
            if priority is not None and self._max_analyzed <= 1 and pool_size:
                self._single_slot_peer = not self._single_slot_peer
                if not self._single_slot_peer:
                    self._rotation_index = (self._rotation_index + 1) % pool_size
            elif pool_size:
                slots = max(1, self._max_analyzed - (priority is not None))
                self._rotation_index = (self._rotation_index + slots) % pool_size
            self._targets = self._active_page()
            self._current_index = 0
            if not self._targets:
                self._current_freq = None
                self._current_k = None
                self._narrow = None
                self._narrow_key = None
                return
        self._current_freq = self._targets[self._current_index]
        self._refresh_current_channel()

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        if self._targets:
            queue = self._targets[self._current_index + 1 :] + self._targets[: self._current_index]
        else:
            queue = []
        # Target activity is accumulated after _NarrowChannel's decimation,
        # which is normally 50 kS/s; the polyphase bank uses a different rate.
        out_rate = self._sample_rate_hz / max(1, int(round(self._sample_rate_hz / _NARROW_RATE_HZ)))
        targets_status = []
        for freq in self._targets:
            state = self._target_state.get(freq)
            targets_status.append(
                {
                    "frequency_hz": freq,
                    "active_s": (state.active_samples / out_rate) if state and out_rate else 0.0,
                    "attempts": state.attempts if state else 0,
                    "analyzed": state.analyzed if state else False,
                }
            )
        return {
            "active": self._stream is not None,
            "queue": queue,
            "analyzing": self._current_freq,
            "targets": targets_status,
            "candidate_count": len(self._candidate_targets),
            "candidate_frequencies_hz": list(self._candidate_targets),
            "priority_frequency_hz": self._priority_freq,
            "iq": {
                "connected": (
                    self._last_frame_at is not None
                    and (self._clock() - self._last_frame_at) < _IQ_CONNECTED_TIMEOUT_S
                ),
                "frames": self._frames,
                "dropped": self._dropped,
                # complex samples/s actually delivered (None until measured)
                "rate_hz": self._iq_rate_hz,
                "cpu_load": self._cpu_load,
            },
            "max_analyzed": self._max_analyzed,
            "dwell_s": self._dwell_s,
            "last_error": self._last_error,
            "rds": {
                "active": bool(self._want_rds()),
                "analyzing": self._rds_current,
                "targets": list(self._rds_targets),
                "dwell_s": self._rds_dwell_s,
                "paused": self._rds_paused,
                "block_ms": self._rds_block_ms,
                "stations": {
                    str(freq): parser.result() for freq, parser in self._rds_stations.items()
                },
            },
        }

    # -- IQ pump --------------------------------------------------------

    async def _read_frames(self) -> None:
        assert self._stream is not None
        try:
            async for frame in self._stream.frames():
                if (self._retune_blocked
                        or getattr(frame, "received_at", time.time()) < self._accept_after_epoch):
                    continue
                frame_generation = getattr(frame, "generation", None)
                if self._source is not None and frame_generation is not None:
                    if frame_generation < self._source_generation:
                        continue
                    if frame_generation > self._source_generation:
                        # Recover when a replay source changes before the
                        # listener was attached (or a callback was missed).
                        self._invalidate_source_generation(frame_generation)
                now = self._clock()
                self._last_frame_at = now
                # Delivered IQ rate, measured on arrival: SDRconnect can fall
                # short of the span's rate (live 2026-09-26: 6.8 of 10 MS/s,
                # host under load) -- the gaps break RDS/analysis while
                # `dropped` (our own queue) stays 0, so it has to be visible.
                if self._rate_window_start is None:
                    self._rate_window_start, self._rate_window_samples = now, 0
                else:
                    self._rate_window_samples += len(frame.data) // 4
                    elapsed = now - self._rate_window_start
                    if elapsed >= _IQ_RATE_WINDOW_S:
                        self._iq_rate_hz = self._rate_window_samples / elapsed
                        self._rate_window_start, self._rate_window_samples = now, 0
                if self._queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._queue.get_nowait()
                    self._dropped += 1
                self._queue.put_nowait(frame)
        except asyncio.CancelledError:
            raise

    async def _pump(self) -> None:
        try:
            while True:
                frame = await self._queue.get()
                if (self._retune_blocked
                        or getattr(frame, "received_at", time.time()) < self._accept_after_epoch):
                    continue
                frame_generation = getattr(frame, "generation", None)
                if self._source is not None and frame_generation is not None:
                    if frame_generation != self._source_generation:
                        continue
                self._frames += 1
                iq = _frame_to_iq(frame.data)

                async with self._process_lock:
                    if (
                        self._retune_blocked
                        or getattr(frame, "received_at", time.time()) < self._accept_after_epoch
                    ):
                        continue
                    if self._source is not None and frame_generation is not None:
                        desc = self._source.descriptor()
                        # The source may have advanced while this frame waited
                        # in the analyzer queue; never run it under a newer
                        # generation's reset DSP state.
                        if frame_generation != desc.generation:
                            continue
                        if frame_generation != self._processed_source_generation:
                            self._retune_locked(desc.center_hz, desc.sample_rate_hz)
                            self._processed_source_generation = frame_generation
                    gen = self._retune_gen
                    t0 = time.perf_counter()
                    try:
                        result = await asyncio.to_thread(self._process_block, iq)
                    except Exception as exc:  # noqa: BLE001 -- keep the pump alive
                        # A bad block must not end analysis for the session
                        # (review 2026-09-26: an AssertionError killed the
                        # pump while last_error stayed empty).
                        self._last_error = f"{type(exc).__name__}: {exc}"
                        self._reset_accum()
                        self._on_status()
                        continue
                    dt = time.perf_counter() - t0
                    nominal = iq.size / self._sample_rate_hz if self._sample_rate_hz else dt
                    load = dt / nominal if nominal > 0 else 0.0
                    self._cpu_load = 0.3 * load + 0.7 * self._cpu_load
                    if gen != self._retune_gen:
                        continue  # computed for the old span: meaningless now

                    rds_results, self._rds_pending = self._rds_pending, []
                    for frequency_hz, fields in rds_results:
                        self._on_result(frequency_hz, {**fields, "_analyzer_epoch": gen})
                    if result is not None:
                        frequency_hz, fields = result
                        self._on_result(frequency_hz, {**fields, "_analyzer_epoch": gen})
                        self._advance_target()
                    if result is not None or rds_results:
                        self._on_status()
        except asyncio.CancelledError:
            raise

    def _process_block(self, iq: np.ndarray) -> tuple[int, dict] | None:
        """Runs off the event loop (asyncio.to_thread): pure numpy. Always
        channelizes (keeps the delay line warm and cpu_load meaningful even
        between dwells) but only accumulates/analyses the current target."""
        self._process_rds(iq)
        channelized = self._channelizer.process(iq)
        if self._current_k is None or channelized.shape[0] == 0:
            return None
        channel_power_db = 10.0 * np.log10(np.mean(np.abs(channelized) ** 2, axis=0) + 1e-20)
        band_floor_db = float(np.percentile(channel_power_db, _BAND_FLOOR_PERCENTILE))
        self._band_floor_db = (
            band_floor_db
            if self._band_floor_db is None
            else (1.0 - _BAND_FLOOR_EMA_ALPHA) * self._band_floor_db
            + _BAND_FLOOR_EMA_ALPHA * band_floor_db
        )

        target = self._narrow.process(iq)
        if target.size:
            self._accum.append(target)
            self._accum_len += target.size

        out_rate = self._narrow.out_rate
        needed = int(round(self._dwell_s * out_rate))
        if self._accum_len < needed:
            return None

        buf = np.concatenate(self._accum)[:needed]
        result = self._analyze_target(buf, out_rate)
        return result

    # -- RDS dwell (worker thread) ----------------------------------------

    def _rds_stop_dwell(self) -> None:
        self._rds_current = None
        self._rds_tune_hz = None
        self._rds_rx = None
        self._rds_elapsed_s = 0.0

    def _rds_pick(self, usable: list[int]) -> int:
        """Least-recently-dwelt channel first (never-dwelt ones before all,
        in target order = strongest first). An index into `usable` would not
        do: the list is re-sorted by SNR every pass, so a rotating index
        skipped some stations for many rounds and revisited others (live
        2026-09-26: 98.9 targeted, never dwelt)."""

        next_seq = self._rds_seq + 1

        def age(channel_hz: int) -> float:
            visit = self._rds_visits.get(channel_hz)
            if visit is None:
                return float("inf")
            last_seq, dwells = visit
            parser = self._rds_stations.get(channel_hz)
            dead = dwells >= _RDS_DEAD_DWELLS and (parser is None or parser.groups == 0)
            return (next_seq - last_seq) / (_RDS_DEAD_SLOWDOWN if dead else 1)

        best = max(usable, key=age)  # max() keeps the first on ties
        self._rds_seq = next_seq
        visit = self._rds_visits.setdefault(best, [0, 0])
        visit[0] = self._rds_seq
        visit[1] += 1
        return best

    def _process_rds(self, iq: np.ndarray) -> None:
        """One RDS station at a time, `rds_dwell_s` of raw IQ each, rotating
        over the RDS targets. Results go to `_rds_pending` (emitted by the
        pump on the event loop) at the end of a dwell and as soon as a new
        PS appears mid-dwell."""
        targets = self._rds_targets  # snapshot: set_rds_targets swaps the list
        if not self._want_rds() or not targets:
            if self._rds_current is not None:
                self._rds_stop_dwell()
            return
        half_span = self._sample_rate_hz / 2 - _RDS_EDGE_MARGIN_HZ
        usable: list[int] = []
        for f in targets:
            channel_hz = fm_channel_hz(f)
            if abs(channel_hz - self._center_hz) <= half_span and channel_hz not in usable:
                usable.append(channel_hz)
        if not usable:
            self._rds_stop_dwell()
            return
        # A running dwell is finished even if its channel briefly left the
        # target list (labels flicker on weak stations): aborting emitted
        # nothing and wasted the dwell. Only leaving the span stops it.
        if self._rds_current is not None and abs(self._rds_current - self._center_hz) > half_span:
            self._rds_stop_dwell()
        if self._rds_current is None:
            self._rds_current = self._rds_pick(usable)
            self._rds_tune_hz = self._rds_current
            self._rds_elapsed_s = 0.0
            known = self._rds_stations.get(self._rds_tune_hz)
            self._rds_last_ps = known.ps if known is not None else None

        if self._rds_paused:
            self._rds_paused = self._cpu_load >= _RDS_CPU_RESUME
        else:
            self._rds_paused = self._cpu_load > _RDS_CPU_PAUSE
        if self._rds_paused:
            # Skipped blocks break the demod's phase/clock continuity; a
            # fresh receiver (same station accumulator) picks up on resume.
            self._rds_rx = None
            return

        dropped = self._dropped
        if dropped != self._rds_seen_dropped:
            # IQ frames were dropped before this one: the demod's pilot
            # phase, symbol clock and block sync no longer line up with the
            # signal. A fresh receiver re-acquires within ~2 blocks; the old
            # one would spend several blocks decoding garbage first.
            self._rds_seen_dropped = dropped
            self._rds_rx = None
        assert self._rds_tune_hz is not None
        parser = self._rds_stations.setdefault(self._rds_tune_hz, RdsParser())
        if self._rds_rx is None:
            self._rds_rx = RdsReceiver(
                self._sample_rate_hz, self._rds_tune_hz - self._center_hz, parser
            )
        t0 = time.perf_counter()
        self._rds_rx.process(iq)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        self._rds_block_ms = (
            dt_ms if self._rds_block_ms == 0.0 else (0.3 * dt_ms + 0.7 * self._rds_block_ms)
        )
        self._rds_elapsed_s += iq.size / self._sample_rate_hz

        ps = parser.ps
        done = self._rds_elapsed_s >= self._rds_dwell_s
        if done or (ps is not None and ps != self._rds_last_ps):
            self._rds_last_ps = ps
            self._rds_pending.append(
                (self._rds_current, {"rds": parser.result(), "analyzed_at": time.time()})
            )
        if done:
            self._rds_stop_dwell()

    def _denoised_env_cv(self, x: np.ndarray) -> float:
        """envelope_cv with the receiver noise's share taken out. Noise alone
        makes a constant-envelope FM signal's envelope swing by ~sqrt(N/2)/A:
        at 8 dB SNR that's ~0.45, over classify_modulation's 0.25 FM limit
        (live 2026-09-26: NOAA 162.400 read "unknown"). The band floor gives
        the noise power N; this channel is ~2 dB wider than a channelizer
        slot (_NARROW_NOISE_EXCESS_DB). Without a floor: plain envelope_cv."""
        raw = envelope_cv(x)
        if self._band_floor_db is None or x.size == 0:
            return raw
        noise = 10.0 ** ((self._band_floor_db + _NARROW_NOISE_EXCESS_DB) / 10.0)
        mag = np.abs(x)
        signal = float(np.mean(mag**2)) - noise
        if signal <= 0.0:
            return raw
        var = max(0.0, float(np.var(mag)) - noise / 2.0)
        return float(np.sqrt(var / signal))

    def _gate_active_blocks(
        self, buf: np.ndarray, out_rate: float, state: _TargetState
    ) -> tuple[float, list[tuple[np.ndarray, float]]]:
        """Split `buf` into _ACTIVITY_BLOCK_S blocks, update `state`'s
        persisted noise floor from this dwell's block powers, and return
        (active_fraction, [(normalised active run, its envelope cv^2)]) --
        see the module docstring comment above _ACTIVITY_BLOCK_S."""
        block_len = max(1, int(round(_ACTIVITY_BLOCK_S * out_rate)))
        n_blocks = buf.size // block_len
        if n_blocks == 0:
            return 0.0, []
        blocks = [buf[i * block_len : (i + 1) * block_len] for i in range(n_blocks)]
        powers_db = np.array([_block_power_db(b) for b in blocks])
        new_floor = float(np.percentile(powers_db, _NOISE_FLOOR_PERCENTILE))
        if state.noise_floor_db is None:
            state.noise_floor_db = new_floor
        else:
            state.noise_floor_db = (
                1.0 - _NOISE_FLOOR_EMA_ALPHA
            ) * state.noise_floor_db + _NOISE_FLOOR_EMA_ALPHA * new_floor
        floor_db = state.noise_floor_db
        if self._band_floor_db is not None:
            floor_db = min(floor_db, self._band_floor_db)
        active_mask = powers_db > (floor_db + _ACTIVITY_MARGIN_DB)
        # Contiguous active blocks form runs (bursts). Each run is normalised
        # to unit power before being stitched to others: bursts from
        # different senders (AIS ships, APRS stations) or a fading signal
        # arrive at different levels, and stitching them raw read as a huge
        # envelope swing (AIS: 0.7, "unknown"). The envelope measure is taken
        # per run first, noise-corrected, while the absolute level is known.
        runs: list[tuple[np.ndarray, float]] = []
        i = 0
        while i < n_blocks:
            if not active_mask[i]:
                i += 1
                continue
            j = i
            while j < n_blocks and active_mask[j]:
                j += 1
            if i > 0 and j < n_blocks:
                state.gaps += 1  # keyed off-and-on again inside this dwell
                state.on_runs.append((j - i) * block_len / out_rate)
            # A burst's first and last blocks are part ramp/part noise (a
            # transmitter keying up, a block straddling the edge); on short
            # bursts (AIS: ~5 blocks) they dominated the envelope measure.
            lo, hi = (i + 1, j - 1) if j - i >= 3 else (i, j)
            if i == 0:
                lo = i  # the dwell started mid-burst: no edge to trim here
            if j == n_blocks:
                hi = j
            run = np.concatenate(blocks[lo:hi])
            cv2 = self._denoised_env_cv(run) ** 2
            power = float(np.mean(np.abs(run) ** 2))
            if power > 0.0:
                runs.append(((run / np.sqrt(power)).astype(np.complex64), cv2))
            i = j
        return float(np.mean(active_mask)), runs

    def _trim_target_accum(self, state: _TargetState, out_rate: float, now: float) -> None:
        max_samples = int(round(_ACCUM_MAX_S * out_rate))
        while state.chunks and now - state.chunks[0][0] > _ACCUM_MAX_AGE_S:
            _, chunk, _ = state.chunks.popleft()
            state.active_samples -= chunk.size
        while state.active_samples > max_samples and state.chunks:
            _, chunk, _ = state.chunks.popleft()
            state.active_samples -= chunk.size

    def _analyze_target(self, buf: np.ndarray, out_rate: float) -> tuple[int, dict]:
        """Activity-gated entry point: only ever feeds accumulated *active*
        samples to the feature extractors (see the module comment above
        _ACTIVITY_BLOCK_S) -- a target stays "waiting for signal" (but stays
        in rotation, see set_targets) until enough active audio has piled up
        across visits."""
        target_hz = self._current_freq
        assert target_hz is not None
        state = self._target_state.setdefault(target_hz, _TargetState())
        state.attempts += 1

        # Preserve actual key-up gaps: the generic active-only accumulator
        # intentionally discards them and introduces joins between runs.
        cw = cw_evidence(_fine_mix(buf, -self._current_residual_hz, out_rate), out_rate)
        if cw is not None:
            measured = int(round((state.tune_hz or target_hz) + cw["offset_hz"]))
            state.analyzed = True
            return target_hz, {
                "modulation": {"kind": "ook", "confidence": 0.75, "features": {"cw_evidence": cw}},
                "family": {
                    "name": "CW (tentative)",
                    "suggested_decoders": ["cw"],
                    "alternatives": ["other slow keyed carrier"],
                    "next_action": "decode CW / verify Morse text",
                    "confidence": 0.75,
                },
                "voice": {"kind": "data", "confidence": 0.75, "score": 0.0},
                "keying": cw,
                "symbol_rate_hz": None,
                "symbol_rate_raw_hz": None,
                "symbol_rate_snapped_hz": None,
                "symbol_rate_method": "morse_on_run_timing",
                "symbol_rate_tolerance": _SYMBOL_RATE_TOLERANCE,
                "measured_hz": measured,
                "analyzed_at": time.time(),
            }

        active_fraction, runs = self._gate_active_blocks(buf, out_rate, state)
        now = self._clock()
        for run, cv2 in runs:
            state.chunks.append((now, run, cv2))
            state.active_samples += run.size
        self._trim_target_accum(state, out_rate, now)

        active_s = state.active_samples / out_rate if out_rate else 0.0
        needed_active = int(round(self._min_active_s * out_rate))
        if state.active_samples < needed_active:
            fields = {
                "modulation": {
                    "kind": "unknown",
                    "confidence": 0.0,
                    "features": {"active_fraction": active_fraction, "active_s": active_s},
                },
                "symbol_rate_hz": None,
                "symbol_rate_raw_hz": None,
                "symbol_rate_snapped_hz": None,
                "symbol_rate_tolerance": _SYMBOL_RATE_TOLERANCE,
                "symbol_rate_method": None,
                "family": None,
                "voice": None,
                "analyzed_at": None,
                "note": "waiting for signal",
            }
            return target_hz, fields

        state.analyzed = True
        active_buf = np.concatenate([chunk for _, chunk, _ in state.chunks])
        sizes = np.array([chunk.size for _, chunk, _ in state.chunks], dtype=np.float64)
        cv2s = np.array([cv2 for _, _, cv2 in state.chunks])
        env_cv = float(np.sqrt(np.sum(sizes * cv2s) / np.sum(sizes)))
        mix_hz = state.tune_hz if state.tune_hz is not None else target_hz
        refined_hz, fields = self._analyze_active_signal(
            int(round(mix_hz)),
            active_buf,
            out_rate,
            active_fraction,
            active_s,
            env_cv,
            state.gaps,
            list(state.on_runs),
        )
        if abs(refined_hz - mix_hz) > _RETUNE_HZ:
            # The label (a 512-bin spectrum centroid) was off by more than
            # the channel can tolerate -- AIS B labelled 10.8 kHz away sat on
            # the filter edge. Re-centre on the measurement for the next
            # dwells; what was accumulated at the old centre is dropped.
            state.tune_hz = float(refined_hz)
            state.chunks.clear()
            state.active_samples = 0
            self._refresh_current_channel()
        # Keyed by the target (the label's frequency), not the measurement:
        # Survey matches results to labels by frequency, and re-centring
        # moved the measured value more than a bin from the label -- the row
        # stayed "analyzing…" for minutes (live 2026-09-26, NOAA 162.424).
        fields["measured_hz"] = refined_hz
        return target_hz, fields

    def _analyze_active_signal(
        self,
        target_hz: int,
        active_buf: np.ndarray,
        out_rate: float,
        active_fraction: float,
        active_s: float,
        env_cv: float | None = None,
        gaps: int = 0,
        on_runs: list[float] | None = None,
    ) -> tuple[int, dict]:
        # Fine-mix by the channel-vs-target residual first (the same
        # correction NFMChannel applies), then refine with a measured PSD
        # centroid on top -- the spectrum tracker's target frequency is only as precise as a
        # 512-bin spectrum. Applied over the accumulated *active* samples
        # only (possibly stitched together across several dwell visits).
        mixed = _fine_mix(active_buf, -self._current_residual_hz, out_rate)
        offset_hz = _psd_centroid_hz(mixed, out_rate)
        refined = _fine_mix(mixed, offset_hz, out_rate)
        refined_hz = int(round(target_hz + offset_hz))

        features = {
            "env_cv": env_cv if env_cv is not None else self._denoised_env_cv(refined),
            "ifreq_std_hz": ifreq_std_hz(refined, out_rate),
            "occ_bw_hz": occupied_bw_hz(refined, out_rate),
            "symmetry": spectral_symmetry(refined),
            "active_fraction": active_fraction,
            "active_s": active_s,
        }
        # 2-level FSK evidence: GMSK 9600 read psk_like before (synthetic corpus).
        crossing_rate, crossing_fit = crossing_rate_hz(refined, out_rate)
        features["ifreq_bimodality"] = ifreq_bimodality(refined, out_rate)
        features["crossing_fit"] = crossing_fit
        clock_rate, clock_db = fsk_clock_line(refined, out_rate)
        features["fsk_clock_line_db"] = clock_db
        raw_rate, line_strength = symbol_rate_hz(refined, out_rate)
        rate_method = "clock_line" if raw_rate is not None else None
        modulation = classify_modulation(
            {**features, "fsk_line_strength": line_strength if raw_rate is not None else 0.0}
        )
        timing = keying_timing(on_runs or [])
        # Active-only samples lose the key-up envelope. Strong, slow Morse
        # timing plus a narrow occupied spectrum can recover keyed-carrier
        # evidence even when residual phase noise resembles FM.
        cw_timing = (
            timing.get("fits_cw") is True
            and 0.02 <= (timing.get("unit_s") or 0) <= 0.3
            and 0 < features["occ_bw_hz"] <= 500
            and gaps >= _OOK_MIN_GAPS
        )
        if cw_timing:
            modulation = {"kind": "ook", "confidence": 0.75}
        if modulation["kind"] == "carrier" and gaps >= _OOK_MIN_GAPS:
            # A carrier that keeps going quiet and coming back is keyed:
            # CW/OOK. (Runs are normalised, so the on/off can't show up in
            # env_cv any more -- the gate's gap count carries it.)
            modulation = {"kind": "ook", "confidence": 0.75}
        crossing_snapped = snap_symbol_rate(crossing_rate)
        if (
            modulation["kind"] == "fm_fsk"
            and features["ifreq_bimodality"] >= BIMODAL_GATE
            and crossing_snapped is not None
            and snap_symbol_rate(raw_rate) != crossing_snapped
        ):
            # A 2-level FSK's frequency crossings sit on the symbol grid; the
            # clock-line search's lag resolution can't resolve 9600 Bd at a
            # few samples/symbol (no line, an off-grid one, or its 4800
            # subharmonic on clean GMSK -- synthetic). With 2-level
            # evidence the crossing grid wins.
            raw_rate, line_strength, rate_method = crossing_rate, crossing_fit, "ifreq_crossings"
        if (
            modulation["kind"] == "fm_fsk"
            and features["ifreq_bimodality"] >= 0.5
            and clock_rate is not None
            and rate_method != "ifreq_crossings"
        ):
            # Four-level FSK has a sharp transition clock but need not fit
            # the two-level crossing grid. Its line can also coexist with
            # lower-rate repetitions in a control stream.
            raw_rate = clock_rate
            line_strength = min(1.0, clock_db / 25.0)
            rate_method = "fsk_clock_line"
        snapped = snap_symbol_rate(raw_rate)
        symbol_rate_field = snapped if snapped is not None else raw_rate
        if modulation["kind"] == "carrier":
            # Nothing is keyed on a bare carrier: a "rate" here is noise.
            symbol_rate_field = raw_rate = snapped = rate_method = None
        afsk = False
        if modulation["kind"] == "fm_fsk":
            audio = _discriminator_audio(refined, out_rate)
            if bell202_score(audio, out_rate) >= _BELL202_MIN_SCORE:
                # Audio-FSK over FM (APRS/packet): the clock is in the audio.
                # raw stays the RF search's result; "snapped" is the tones'.
                afsk = True
                symbol_rate_field = snapped = 1200.0
                rate_method = "bell202_tones"
                line_strength = max(line_strength, 0.8)

        if cw_timing:
            symbol_rate_field = raw_rate = snapped = None
            rate_method = "morse_on_run_timing"
            line_strength = 1.0

        # bw_class is always "not wide" here: Survey never puts wfm/
        # wide_digital labels into the target list (see set_dimensions/
        # _analyzer_targets), so family_for's wideband bypass can't apply.
        keying = keying_timing(on_runs or []) if modulation["kind"] == "ook" else None
        family = family_for(
            modulation["kind"], symbol_rate_field, "nfm", features["occ_bw_hz"], keying=keying
        )
        if afsk:
            # The tones pin it down beyond the 1200-baud FSK family (POCSAG
            # is direct FSK: no 1200/2200 Hz audio tones).
            family = {
                "name": "AX.25 AFSK 1200 (APRS / packet)",
                "suggested_decoders": ["aprs"],
                "alternatives": ["other Bell 202 AFSK"],
                "next_action": "decode aprs",
            }
        if family is not None:
            family = {
                **family,
                "confidence": float(np.clip(line_strength * modulation["confidence"], 0.0, 1.0)),
            }

        fields = {
            "modulation": {
                "kind": modulation["kind"],
                "confidence": modulation["confidence"],
                "features": features,
            },
            "symbol_rate_hz": symbol_rate_field,
            "symbol_rate_raw_hz": raw_rate,
            "symbol_rate_snapped_hz": snapped,
            "symbol_rate_tolerance": _SYMBOL_RATE_TOLERANCE,
            "symbol_rate_method": rate_method,
            "family": family,
            "voice": None,
            "analyzed_at": time.time(),
        }
        if keying is not None:
            fields["keying"] = keying
        # voice_score only makes sense on a signal that could plausibly carry
        # speech (fm_fsk, or unknown-but-real -- OOK/AM/PSK-like/carrier have
        # no discriminator structure resembling speech either way) and needs
        # at least half a second of active audio to mean anything.
        wants_voice_signal = modulation["kind"] in ("fm_fsk", "unknown")
        digital_evidence = afsk or (
            modulation["kind"] == "fm_fsk"
            and (
                (features["ifreq_bimodality"] >= BIMODAL_GATE and crossing_snapped is not None)
                or rate_method == "fsk_clock_line"
            )
        )
        # Symbol-grid and two-level evidence outrank the speech-envelope heuristic.
        # Bursty digital audio can score as speech after low-pass filtering.
        if digital_evidence and self._want_voice():
            fields["voice"] = {"kind": "data", "confidence": 0.9, "score": 0.0}
        elif self._want_voice() and wants_voice_signal and active_s >= _MIN_VOICE_ACTIVE_S:
            audio = _discriminator_audio(refined, out_rate)
            # Voice band only, like a receiver's audio: the discriminator's
            # noise rises with frequency, and the 3-12.5 kHz hiss diluted the
            # speech-band and syllabic ratios -- NOAA weather voice scored
            # "data" (0.18-0.48) until low-passed (0.78-1.0), live 2026-09-26.
            audio = np.convolve(audio, _VOICE_LOWPASS(out_rate), mode="same")
            voice = voice_score(audio, rate=out_rate)
            fields["voice"] = {
                "kind": voice["kind"],
                "confidence": voice["confidence"],
                "score": voice["score"],
            }
            if voice["kind"] == "voice" and fields["modulation"]["kind"] == "unknown":
                # Voice through a discriminator with a wide frequency swing is
                # FM, even when fading/noise pushed the envelope swing past
                # classify_modulation's FM limit (AM voice barely moves in
                # frequency). Live 2026-09-26: weak NOAA 162.400, "unknown".
                if features["ifreq_std_hz"] > _FM_VOICE_MIN_IFREQ_STD_HZ:
                    fields["modulation"] = {
                        **fields["modulation"],
                        "kind": "fm_fsk",
                        "confidence": 0.7,
                    }
            if voice["kind"] == "voice":
                # Speech has no symbol clock: whatever line the rate search
                # found (often its 9600 cap) is noise, and so is the FSK
                # family it implied.
                fields["symbol_rate_hz"] = None
                fields["symbol_rate_raw_hz"] = None
                fields["symbol_rate_snapped_hz"] = None
                fields["symbol_rate_method"] = None
                if fields["modulation"]["kind"] == "fm_fsk":
                    fields["family"] = {
                        "name": "speech-like NFM audio (tentative)",
                        "suggested_decoders": [],
                        "alternatives": ["digital modulation with speech-like envelope"],
                        "next_action": "listen (NFM) / verify with recording or decode",
                        "confidence": voice["confidence"],
                    }
                else:
                    # Speech-like discriminator audio on a signal that isn't
                    # FM-like: don't name the modulation we haven't tested.
                    fields["family"] = {
                        "name": "speech-like audio (demodulation untested)",
                        "suggested_decoders": [],
                        "alternatives": ["NFM voice (analog)"],
                        "next_action": "listen / tested path: NFM discriminator",
                        "confidence": voice["confidence"],
                    }
        return refined_hz, fields
