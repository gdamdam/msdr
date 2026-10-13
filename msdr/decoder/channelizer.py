"""Polyphase filterbank channelizer + per-channel NFM demod chain (P2 band decode).

`PolyphaseChannelizer` turns one wideband IQ stream into M narrowband channels
spaced `spacing_hz` apart, 2x-oversampled (hop = M/2 input samples per output
row) so a signal that straddles a channel boundary is still fully inside one
of the two overlapping channels that see it. `NFMChannel` then takes one
channel's column and runs the same demod chain P1 uses per-decoder (fine mix
-> discriminator -> Resampler -> Normalizer -> to_s16), just starting from a
channelizer column instead of SDRconnect's own audio stream.
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from msdr.decoder.pcm import Normalizer, Resampler, lowpass_taps, to_s16

# NBFM standard peak deviation (Carson's rule with the P1 12,500 Hz default
# bandwidth: ~1,250 Hz audio + 5,000 Hz deviation each side): normalises the
# discriminator's Hz output to roughly +-1, the range Normalizer/to_s16 expect.
_MAX_DEVIATION_HZ = 5000.0


class PolyphaseChannelizer:
    """2x-oversampled polyphase filterbank: M channels spaced fs/M apart, output at rate 2*fs/M."""

    def __init__(self, fs: int, spacing_hz: int = 12500, taps_per_branch: int = 8):
        m = round(fs / spacing_hz)
        if m % 2:  # hop = M/2 must land on an integer sample count
            m += 1
        self.M = m
        self.spacing_hz = fs / m  # exact division of fs, may differ slightly from the request
        self.out_rate = 2 * self.spacing_hz
        self._taps_per_branch = taps_per_branch
        self._hop = m // 2
        self._l = taps_per_branch * m

        # Windowed-sinc prototype, cutoff at half a channel's spacing (spec
        # P2 "Correctness notes": passband +-spacing/2, rolling off out to
        # +-spacing, 2*spacing wide == out_rate). Normalised so sum(h) == 1
        # (unity DC gain) -- the *unnormalised* np.fft.fft over the M-length
        # commutator vector then supplies exactly the M-fold gain a tone
        # sitting on a bin needs to come out at unit amplitude.
        fc_norm = 0.5 / m
        n = np.arange(self._l, dtype=np.float64) - (self._l - 1) / 2
        h = np.sinc(2 * fc_norm * n) * np.hamming(self._l)
        h = (h / h.sum()).astype(np.float32)
        self._h = h.reshape(taps_per_branch, m)

        # Carried across process() calls so a frame boundary doesn't reset
        # the filter or the odd/even hop parity (like Resampler._filt_state).
        self._buf = np.zeros(self._l - self._hop, dtype=np.complex64)
        self._hop_index = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        """complex64 (n,) -> (hops, M) complex64; keeps the delay line across calls."""
        combined = np.concatenate([self._buf, np.asarray(x, dtype=np.complex64)])
        n_hops = max(0, (combined.size - self._l) // self._hop + 1)
        if n_hops == 0:
            self._buf = combined
            return np.zeros((0, self.M), dtype=np.complex64)

        # sliding_window_view gives every offset; keep every `hop`-th one --
        # window i is combined[i*hop : i*hop+L], the polyphase commutator's
        # input for that hop.
        windows = sliding_window_view(combined, self._l)[:: self._hop][:n_hops]
        branches = windows.reshape(n_hops, self._taps_per_branch, self.M)
        commutator = np.sum(branches * self._h[None, :, :], axis=1)
        out = np.fft.fft(commutator, axis=-1).astype(np.complex64)

        # A hop of M/2 (not a multiple of M) shifts phase by pi*k between
        # consecutive hops; undo it on the odd ones so the fine mixer in
        # NFMChannel sees a clean, continuously-rotating carrier.
        odd = ((self._hop_index + np.arange(n_hops)) % 2).astype(bool)
        sign = ((-1.0) ** np.arange(self.M)).astype(np.float32)
        out[odd] *= sign[None, :]

        self._hop_index += n_hops
        self._buf = combined[n_hops * self._hop :]
        return out

    def channel_center_hz(self, k: int, center_hz: float) -> float:
        """Absolute centre of channel k (k wraps: k >= M/2 -> negative offset)."""
        offset = k if k < self.M / 2 else k - self.M
        return center_hz + offset * self.spacing_hz

    def channel_for(self, freq_hz: float, center_hz: float) -> tuple[int, float]:
        """(k, residual_hz) for freq_hz; |residual_hz| <= spacing_hz/2."""
        offset = freq_hz - center_hz
        k = int(round(offset / self.spacing_hz)) % self.M
        residual_hz = freq_hz - self.channel_center_hz(k, center_hz)
        return k, residual_hz


class NFMChannel:
    """One channel's demod chain at the channelizer's out_rate."""

    def __init__(self, out_rate: float, residual_hz: float, target_rate: int = 22050):
        self.out_rate = out_rate
        self.residual_hz = residual_hz
        # NCO phase for the fine mixer and the discriminator's inter-block
        # sample, both carried across calls (frame boundaries mustn't click).
        self._phase = 0.0
        self._phase_step = -2 * np.pi * residual_hz / out_rate
        self._prev: complex | None = None
        self._resampler = Resampler(int(round(out_rate)), target_rate)
        self._normalizer = Normalizer(rate=target_rate)

    def process(self, ch: np.ndarray) -> bytes:
        if ch.size == 0:
            return b""
        n = np.arange(ch.size, dtype=np.float64)
        mixer = np.exp(1j * (self._phase + self._phase_step * n)).astype(np.complex64)
        self._phase = float((self._phase + self._phase_step * ch.size) % (2 * np.pi))
        mixed = ch.astype(np.complex64) * mixer

        if self._prev is not None:
            mixed = np.concatenate([[self._prev], mixed])
        self._prev = complex(mixed[-1])

        # Standard complex discriminator: angle between consecutive samples
        # is the instantaneous phase step, i.e. frequency in rad/sample.
        prod = mixed[1:] * np.conj(mixed[:-1])
        audio = np.angle(prod).astype(np.float32)
        audio *= np.float32(self.out_rate / (2 * np.pi * _MAX_DEVIATION_HZ))

        resampled = self._resampler.process(audio)
        normalized = self._normalizer.process(resampled)
        return to_s16(normalized)


class AMChannel:
    """One channel's AM demod chain at the channelizer's out_rate (airband).

    Same interface and output format as NFMChannel (mono s16 at target_rate)
    so the band pump feeds either kind of channel to its backends unchanged.
    Unlike NFM, which relies on the decoder to cope with noise, AM carries a
    squelch: with no carrier the channel emits silence (not nothing, so decoder
    timing stays continuous) rather than amplified noise through the AGC.
    """

    # Squelch decisions are made per ~20 ms chunk: short enough to catch a
    # transmission start, long enough for a stable power estimate.
    _CHUNK_S = 0.02
    _HYSTERESIS_DB = 3.0
    _HANG_CHUNKS = 5  # keep audio ~100 ms after the carrier drops (word gaps)
    _NUM_TAPS = 101

    def __init__(
        self,
        out_rate: float,
        residual_hz: float,
        target_rate: int = 22050,
        bandwidth_hz: float = 8000.0,
        squelch_db: float = 10.0,
    ):
        self.out_rate = out_rate
        self.residual_hz = residual_hz
        self.bandwidth_hz = bandwidth_hz
        self.squelch_db = squelch_db
        self.squelch_open = False
        self._phase = 0.0
        self._phase_step = -2 * np.pi * residual_hz / out_rate
        # Channel filter: bandwidth_hz is the full width (+-4 kHz for 25 kHz
        # spacing, +-3 kHz for 8.33 kHz), which also rejects the neighbouring
        # channel's carrier that the 12.5 kHz-wide PFB bin lets through.
        fc = (bandwidth_hz / 2) / out_rate
        self._h = lowpass_taps(fc, self._NUM_TAPS).astype(np.float32)
        self._filt_state = np.zeros(self._NUM_TAPS - 1, dtype=np.complex64)
        self._chunk = max(1, int(round(out_rate * self._CHUNK_S)))
        self._carrier: float | None = None
        self._dc = 0.0
        self._hang = 0
        self._resampler = Resampler(int(round(out_rate)), target_rate)
        self._normalizer = Normalizer(rate=target_rate)

    def process(self, ch: np.ndarray) -> bytes:
        if ch.size == 0:
            return b""
        n = np.arange(ch.size, dtype=np.float64)
        mixer = np.exp(1j * (self._phase + self._phase_step * n)).astype(np.complex64)
        self._phase = float((self._phase + self._phase_step * ch.size) % (2 * np.pi))
        mixed = ch.astype(np.complex64) * mixer

        combined = np.concatenate([self._filt_state, mixed])
        filtered = np.convolve(combined, self._h, mode="valid").astype(np.complex64)
        self._filt_state = combined[-(self._NUM_TAPS - 1) :]

        envelope = np.abs(filtered)
        audio = np.zeros(ch.size, dtype=np.float32)
        for start in range(0, ch.size, self._chunk):
            stop = min(start + self._chunk, ch.size)
            if not self._update_squelch(filtered[start:stop], envelope[start:stop]):
                continue
            seg = envelope[start:stop]
            # Dividing by the tracked carrier is the AGC for modulation
            # depth; it also leaves the carrier at 1.0 so "- 1" strips DC.
            audio[start:stop] = seg / max(self._carrier or 0.0, 1e-9) - 1.0
            self._dc += 0.05 * (float(audio[start:stop].mean()) - self._dc)
            audio[start:stop] -= self._dc

        resampled = self._resampler.process(audio)
        normalized = self._normalizer.process(resampled)
        return to_s16(normalized)

    def _update_squelch(self, filtered: np.ndarray, envelope: np.ndarray) -> bool:
        """Carrier-to-noise estimate for one chunk; True while audio passes.

        Noise density is the median bin level of the filtered chunk's spectrum
        inside the passband (carrier bins excluded). Measuring what the filter
        removed instead would count a strong adjacent 8.33 kHz channel as noise
        and mute a good carrier; the median also shrugs off sparse sidebands.
        """
        p_in = float(np.mean(envelope**2))
        n = filtered.size
        w = np.hanning(n)
        spec = np.abs(np.fft.fft(filtered * w)) ** 2 / float(np.sum(w**2))
        freqs = np.fft.fftfreq(n, 1.0 / self.out_rate)
        inband = np.abs(freqs) <= 0.4 * self.bandwidth_hz  # stay off the filter roll-off
        peak = int(np.argmax(np.where(inband, spec, 0.0)))
        inband[np.arange(peak - 3, peak + 4) % n] = False  # carrier + window leakage
        if not inband.any():
            return self.squelch_open
        # For white noise the median of exponential-distributed bin power is ln2 * mean.
        density = float(np.median(spec[inband])) / np.log(2)
        noise_in = max(density * self.bandwidth_hz / self.out_rate, 1e-15)
        snr_db = 10 * np.log10(max(p_in - noise_in, 1e-15) / noise_in)
        level = self.squelch_db - (self._HYSTERESIS_DB if self.squelch_open else 0.0)
        if snr_db >= level:
            self.squelch_open = True
            self._hang = self._HANG_CHUNKS
            mean_env = float(envelope.mean())
            if self._carrier is None:
                self._carrier = mean_env
            else:
                self._carrier += 0.2 * (mean_env - self._carrier)
        elif self._hang > 0:
            self._hang -= 1
        else:
            self.squelch_open = False
        return self.squelch_open
