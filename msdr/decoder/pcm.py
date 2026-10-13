"""PCM plumbing between SDRconnect's audio stream and decoder backends.

SDRconnect delivers int16 stereo (L=R) at a rate that must be measured, not
assumed (spec "Measured facts"); multimon-ng/minimodem want mono s16 at a
fixed 22,050 Hz. This module does stereo->mono, streaming resample, a slow
AGC so weak signals aren't lost under a narrow-deviation demodulator, and the
WAV-header trick minimodem needs on a headerless stdin stream.
"""

import struct

import numpy as np

# Rates the manager snaps a measured throughput to (spec "Audio format").
_STANDARD_RATES = (16000, 22050, 24000, 32000, 44100, 48000)


def lowpass_taps(cutoff_norm: float, n_taps: int) -> np.ndarray:
    """Hamming-windowed-sinc low-pass, unity DC gain. `cutoff_norm` is cutoff / sample rate."""
    n = np.arange(n_taps, dtype=np.float64) - (n_taps - 1) / 2
    h = 2 * cutoff_norm * np.sinc(2 * cutoff_norm * n) * np.hamming(n_taps)
    return h / h.sum()


def cs16_to_iq(data: bytes) -> np.ndarray:
    """Interleaved little-endian int16 I/Q -> complex64 scaled to +-1."""
    raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
    return ((raw[:, 0] + 1j * raw[:, 1]) / 32768.0).astype(np.complex64)


def iq_to_cs16(iq: np.ndarray) -> bytes:
    """complex IQ (full scale = 1.0) -> interleaved little-endian int16, rounded and clipped."""
    packed = np.empty((len(iq), 2), dtype="<i2")
    packed[:, 0] = np.clip(np.rint(iq.real * 32768.0), -32768, 32767).astype("<i2")
    packed[:, 1] = np.clip(np.rint(iq.imag * 32768.0), -32768, 32767).astype("<i2")
    return packed.tobytes()


def to_mono_f32(data: bytes, channels: int) -> np.ndarray:
    """int16 interleaved PCM -> float32 in [-1, 1], first channel only.

    SDRconnect's audio is L=R duplicated stereo, so "first channel" is enough;
    it also means this is correct if a future device ever sends true stereo
    (we just don't down-mix R in).
    """
    samples = np.frombuffer(data, dtype="<i2")
    if channels > 1:
        usable = (samples.size // channels) * channels
        samples = samples[:usable].reshape(-1, channels)[:, 0]
    return (samples.astype(np.float32)) / 32768.0


class Resampler:
    """Streaming FIR-lowpass + linear-interpolation resampler.

    Both the FIR filter memory (`_filt_state`) and the fractional output
    phase (`_next_out_pos`) are carried across calls, so audio fed frame by
    frame resamples exactly as if it had all arrived in one call -- no
    clicks or drift at frame boundaries.
    """

    _NUM_TAPS = 127

    def __init__(self, in_rate: int, out_rate: int):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._h = self._design_filter(in_rate, out_rate, self._NUM_TAPS)
        self._filt_state = np.zeros(len(self._h) - 1, dtype=np.float32)
        self._consumed = 0  # absolute count of input samples filtered so far
        self._next_out_pos = 0.0  # absolute (float) input-sample index of the next output sample

    @staticmethod
    def _design_filter(in_rate: int, out_rate: int, num_taps: int) -> np.ndarray:
        # Anti-aliasing cutoff at 0.45*out_rate (spec), expressed as a
        # fraction of in_rate since the filter runs on the input-rate signal.
        cutoff_hz = 0.45 * min(in_rate, out_rate)
        fc_norm = cutoff_hz / in_rate
        return lowpass_taps(fc_norm, num_taps).astype(np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)
        combined = np.concatenate([self._filt_state, x.astype(np.float32)])
        filtered = np.convolve(combined, self._h, mode="valid").astype(np.float32)
        self._filt_state = combined[-(len(self._h) - 1) :]

        block_start = self._consumed
        block_len = len(filtered)
        self._consumed += block_len

        ratio = self.in_rate / self.out_rate
        out = []
        # Only emit an output sample once both surrounding input samples have
        # arrived; anything past that waits for the next call.
        last_usable = block_start + block_len - 2
        while block_len >= 2 and self._next_out_pos <= last_usable:
            rel = self._next_out_pos - block_start
            i0 = int(rel)
            frac = rel - i0
            out.append(filtered[i0] + frac * (filtered[i0 + 1] - filtered[i0]))
            self._next_out_pos += ratio
        return np.asarray(out, dtype=np.float32)


class Normalizer:
    """Slow AGC: gain = target_peak / running_peak, instant attack, ~1s release.

    Sample-by-sample because the peak follower is recursive (each sample
    depends on the previous one); frames are small (tens of ms of audio) so
    the Python loop cost is negligible next to the subprocess I/O it feeds.
    """

    _FLOOR = 1e-6

    def __init__(self, target_peak: float = 0.5, release_s: float = 1.0, rate: int = 22050):
        self.target_peak = target_peak
        self.rate = rate
        self._release_coeff = float(np.exp(-1.0 / (release_s * rate))) if release_s > 0 else 0.0
        # Seed at target_peak (not 0) so silence at start-up doesn't produce
        # a huge transient gain on the first real sample.
        self._running_peak = target_peak

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        out = np.empty_like(x, dtype=np.float32)
        peak = self._running_peak
        coeff = self._release_coeff
        for i in range(x.shape[0]):
            a = abs(float(x[i]))
            peak = a if a > peak else peak * coeff
            gain = self.target_peak / peak if peak > self._FLOOR else 1.0
            out[i] = x[i] * gain
        self._running_peak = peak
        return out


def to_s16(x: np.ndarray) -> bytes:
    clipped = np.clip(x, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


class RateEstimator:
    """Measures samples/s against wall-clock over the first ~5s of audio.

    SDRconnect never documents its audio rate, so it's inferred from
    throughput (spec "Measured facts").
    """

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self._first_t: float | None = None
        self._last_t: float | None = None
        self._total = 0

    def add(self, n_frames: int, received_at: float) -> float | None:
        """Record n_frames (samples) received at received_at; returns the
        observed rate once >=window_s has elapsed, else None."""
        if self._first_t is None:
            self._first_t = received_at
        self._last_t = received_at
        self._total += n_frames
        elapsed = self._last_t - self._first_t
        if elapsed < self.window_s or elapsed <= 0:
            return None
        return self._total / elapsed

    @staticmethod
    def snap(rate: float) -> int:
        return min(_STANDARD_RATES, key=lambda c: abs(c - rate))


def wav_stream_header(rate: int, channels: int = 1) -> bytes:
    """44-byte streaming WAV header (RIFF/data sizes 0xFFFFFFFF) -- minimodem
    refuses a headerless raw stream on stdin (spec "Measured facts")."""
    bits_per_sample = 16
    block_align = channels * bits_per_sample // 8
    byte_rate = rate * block_align
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 0xFFFFFFFF),
            b"WAVE",
            b"fmt ",
            struct.pack("<I", 16),
            struct.pack("<H", 1),  # PCM
            struct.pack("<H", channels),
            struct.pack("<I", rate),
            struct.pack("<I", byte_rate),
            struct.pack("<H", block_align),
            struct.pack("<H", bits_per_sample),
            b"data",
            struct.pack("<I", 0xFFFFFFFF),
        ]
    )


__all__ = [
    "to_mono_f32",
    "Resampler",
    "Normalizer",
    "to_s16",
    "RateEstimator",
    "wav_stream_header",
]
