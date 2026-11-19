"""Reception check: why a decoder may not be decoding, from the IQ span.

A decoder that stays at "acquiring" says nothing about the cause. This looks
at the span the test ran on -- how the samples sit against full scale, and the
spectrum on and around the channel -- and names what it can see: overload, no
or weak signal, a signal off the tested frequency, a much stronger neighbour,
the receiver's DC spike on the channel, a channel on the span's edge, samples
lost. Each finding is a measurement with a plain sentence and, where there is
an obvious one, a fix; none is a verdict on the decoder.

Pure numpy: `SpanStats` is fed raw IQ blocks off the event loop,
`reception_check` reads it. Thresholds are rules of thumb checked on synthetic
spans only (tests/decoder/test_reception.py); levels are relative, as
everywhere in msdr.
"""

from __future__ import annotations

import numpy as np

_FFT = 4096
_MAX_SEGMENTS = 8  # per block: enough to catch a burst, cheap next to the channelizer
# int16 full scale is 1.0 after conversion; a little under it counts, since
# a clipped ADC rarely lands exactly on the last code.
_CLIP_LEVEL = 0.985
_CLIP_FRACTION = 1e-3
_NO_SIGNAL_DB = 6.0
_WEAK_DB = 12.0
_NEIGHBOUR_DB = 20.0
_NEIGHBOUR_SPAN = 5.0  # channel widths either side that count as "next to"
_DC_SPIKE_DB = 10.0
_EDGE_FRACTION = 0.45  # of the sample rate: the span filter rolls off beyond


class SpanStats:
    """Running clip count and mean / max-hold spectrum of raw IQ blocks."""

    def __init__(self) -> None:
        self.samples = 0
        self.clipped = 0
        self.segments = 0
        self._sum = np.zeros(_FFT)
        self._max = np.zeros(_FFT)
        self._window = np.hanning(_FFT)

    def update(self, iq: np.ndarray) -> None:
        if iq.size < _FFT:
            return
        self.samples += iq.size
        self.clipped += int(
            np.count_nonzero(np.maximum(np.abs(iq.real), np.abs(iq.imag)) >= _CLIP_LEVEL)
        )
        count = min(_MAX_SEGMENTS, iq.size // _FFT)
        starts = np.linspace(0, iq.size - _FFT, count).astype(int)
        for start in starts:
            segment = np.nan_to_num(iq[start : start + _FFT]) * self._window
            power = np.abs(np.fft.fftshift(np.fft.fft(segment))) ** 2
            self._sum += power
            np.maximum(self._max, power, out=self._max)
        self.segments += count

    @property
    def mean(self) -> np.ndarray:
        return self._sum / max(1, self.segments)

    @property
    def peak(self) -> np.ndarray:
        """Max-hold: a short burst still shows after the mean has averaged it away."""
        return self._max


def _db(power: float) -> float:
    return 10.0 * float(np.log10(max(power, 1e-30)))


def _finding(code: str, level: str, text: str, fix: str = "", **extra) -> dict:
    return {"code": code, "level": level, "text": text, "fix": fix, **extra}


def _khz(hz: float) -> str:
    return f"{abs(hz) / 1000:.1f} kHz"


def reception_check(
    stats: SpanStats,
    *,
    sample_rate_hz: float,
    offset_hz: float,
    bandwidth_hz: float = 12_500.0,
    dropped_gaps: int = 0,
) -> list[dict]:
    """Findings for a channel `offset_hz` from the span centre, `bandwidth_hz`
    wide. Each is {code, level: "problem" | "note", text, fix}; problems first.
    Empty when no IQ was seen or nothing stands out."""
    if stats.segments == 0:
        return []
    problems: list[dict] = []
    notes: list[dict] = []
    freqs = (np.arange(_FFT) - _FFT // 2) * (sample_rate_hz / _FFT)
    bin_hz = sample_rate_hz / _FFT
    # Never narrower than three bins: at a wide span one bin can be a
    # quarter of the channel.
    half = max(bandwidth_hz / 2.0, 1.5 * bin_hz)
    peak, mean = stats.peak, stats.mean
    channel = np.abs(freqs - offset_hz) <= half

    clip_fraction = stats.clipped / max(1, stats.samples)
    if clip_fraction >= _CLIP_FRACTION:
        problems.append(
            _finding(
                "overload",
                "problem",
                f"The receiver is overloading: {100 * clip_fraction:.1f}% of samples are at "
                "full scale, which distorts every signal in the span.",
                "Lower the RF gain or switch in attenuation, then test again.",
                clip_fraction=clip_fraction,
            )
        )
    if dropped_gaps > 0:
        problems.append(
            _finding(
                "dropped_samples",
                "problem",
                f"{dropped_gaps} gap(s) in the IQ stream during the test: samples were lost, "
                "which breaks a decoder's timing.",
                "Lower the sample rate, or close other programs using the CPU or the network.",
                dropped_gaps=dropped_gaps,
            )
        )
    if not channel.any() or abs(offset_hz) > _EDGE_FRACTION * sample_rate_hz:
        problems.append(
            _finding(
                "span_edge",
                "problem",
                "The frequency is at the edge of the IQ span, where the receiver's filter "
                "rolls off.",
                "Move the receiver's centre frequency closer to the signal.",
            )
        )
        return problems + notes
    if clip_fraction >= _CLIP_FRACTION:
        # A clipped span's spectrum is full of its own distortion products;
        # nothing measured on it below would mean what it says.
        return problems + notes

    # Max-hold against the max-hold floor: both carry the same upward bias,
    # so the difference is still "how far the channel stood out".
    floor = float(np.median(peak))
    level_db = _db(float(peak[channel].mean())) - _db(floor)

    near_dc = np.abs(freqs) <= 1.5 * bin_hz
    beside_dc = (np.abs(freqs) > 1.5 * bin_hz) & (np.abs(freqs) <= 8 * bin_hz)
    spike_db = _db(float(mean[near_dc].max())) - _db(float(np.median(mean[beside_dc])))
    if abs(offset_hz) <= half and spike_db >= _DC_SPIKE_DB:
        problems.append(
            _finding(
                "dc_spike",
                "problem",
                f"The receiver's own centre spike ({spike_db:.0f} dB over its surroundings) "
                "sits inside the channel.",
                "Move the receiver's centre frequency a few channel widths off the signal.",
            )
        )

    reach = np.abs(freqs - offset_hz) <= _NEIGHBOUR_SPAN * bandwidth_hz
    beside = reach & ~(np.abs(freqs - offset_hz) <= 1.5 * half) & ~near_dc
    if beside.any():
        width = max(1, int(round(2 * half / bin_hz)))
        smooth = np.convolve(peak, np.full(width, 1.0 / width), mode="same")
        strongest = int(np.argmax(np.where(beside, smooth, 0.0)))
        over_db = _db(float(smooth[strongest])) - _db(float(peak[channel].mean()))
        if over_db >= _NEIGHBOUR_DB:
            away = freqs[strongest] - offset_hz
            notes.append(
                _finding(
                    "strong_neighbour",
                    "note",
                    f"A signal {over_db:.0f} dB stronger sits {_khz(away)} "
                    f"{'above' if away > 0 else 'below'}; it can mask this channel or leak "
                    "into it.",
                    "Check that the test is on the right frequency; a narrower channel or a "
                    "lower gain can help.",
                    neighbour_offset_hz=float(away),
                )
            )

    if level_db < _NO_SIGNAL_DB:
        problems.append(
            _finding(
                "no_signal",
                "problem",
                f"Nothing stood out on the channel during the test ({level_db:.0f} dB over "
                "the span's floor).",
                "The station may be off the air or sending in short bursts: test again while "
                "it is transmitting.",
                level_db=level_db,
            )
        )
        return problems + notes
    if level_db < _WEAK_DB:
        notes.append(
            _finding(
                "weak_signal",
                "note",
                f"The signal is weak ({level_db:.0f} dB over the span's floor); digital "
                "decoders usually need more.",
                "Try a better antenna position or more RF gain, short of overloading.",
                level_db=level_db,
            )
        )

    around = np.abs(freqs - offset_hz) <= 1.5 * bandwidth_hz
    weight = np.clip(peak - 2.0 * floor, 0.0, None) * around
    if weight.sum() > 0:
        shift = float((freqs * weight).sum() / weight.sum()) - offset_hz
        if abs(shift) > bandwidth_hz / 4.0:
            problems.append(
                _finding(
                    "off_frequency",
                    "problem",
                    f"The signal is centred {_khz(shift)} "
                    f"{'above' if shift > 0 else 'below'} the tested frequency.",
                    "Test or decode at the measured centre instead.",
                    suggested_offset_hz=shift,
                )
            )
    return problems + notes
