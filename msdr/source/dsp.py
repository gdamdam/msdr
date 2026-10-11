"""Streaming numpy DSP for the virtual receiver (S3 contract §2).

Every class keeps its state (NCO phase, filter history, AGC level, squelch
state) across ``process()`` calls, so a replay chopped into arbitrary IQ
frames demodulates exactly like one long block. ``reset()`` returns a class
to its just-constructed state; the virtual receiver calls it (by rebuilding)
on every source generation change so no filter tail from before a seek leaks
into the audio after it.

Chain: int16 IQ -> ``ChannelExtractor`` (shift + integer decimation stages +
rational resample to 48 kHz or 250 kHz) -> one demodulator -> float32 audio
at 48 kHz. NFM additionally exposes ``.flat`` (decoder tap), WFM ``.mpx``
(RDS input); neither passes through the listening DSP.
"""

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache

import numpy as np

from msdr.survey.rds import MPX_TARGET_RATE_HZ, WFM_DEVIATION_HZ, RdsDemod, RdsParser

VOICE_RATE_HZ = 48_000
MPX_RATE_HZ = MPX_TARGET_RATE_HZ  # 250 kHz, what rds.RdsDemod is tuned for
DEMODULATORS = ("NFM", "AM", "USB", "LSB", "CW", "WFM")

# NBFM peak deviation that maps to +-1.0 on the flat tap -- same scale as
# decoder/channelizer.NFMChannel so S4 decoders see familiar levels.
NFM_DEVIATION_HZ = 5_000.0
# WFM channel filter half-width: rds.WfmChannel's 120 kHz, since MPX with
# RDS at 57 kHz and 75 kHz deviation needs ~+-110 kHz regardless of the
# bandwidth the user typed; the protected (alias-free) band is +-100 kHz.
_WFM_CUTOFF_HZ = 120_000.0
_WFM_PROTECT_HZ = 100_000.0
# SSB/CW: the extractor passes both sides; the sideband filter in the demod
# does the selection, so it needs this much beyond the bandwidth.
_SSB_GUARD_HZ = 200.0
_MAX_STAGE_DECIM = 10
_MAX_STAGES = 3
_MAX_RESAMPLE_UP = 256
_MAX_TAPS = 8191


@dataclass(frozen=True)
class AudioBlock:
    pcm: bytes  # s16 little-endian mono
    rate: int
    generation: int  # source generation the audio belongs to
    sample_index: int  # run-local index of the first IQ sample behind it


def iq16_to_complex(data: bytes) -> np.ndarray:
    """Interleaved int16 I/Q bytes -> complex64 scaled to +-1."""
    a = np.frombuffer(data, dtype="<i2")
    a = a[: a.size - (a.size % 2)].astype(np.float32)
    a *= np.float32(1.0 / 32768.0)
    return a.view(np.complex64)


def to_s16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


# -- filter design -----------------------------------------------------------


def lowpass_taps(cutoff_hz: float, fs: float, transition_hz: float) -> np.ndarray:
    """Hamming windowed sinc, unity DC gain. Length from the Hamming rule
    N ~= 3.3 fs / transition (~53 dB stop band); odd so the delay is an
    integer number of samples."""
    n = int(np.ceil(3.3 * fs / max(transition_hz, 1.0)))
    n = min(max(n, 15), _MAX_TAPS) | 1
    k = np.arange(n, dtype=np.float64) - (n - 1) / 2
    h = np.sinc(2.0 * cutoff_hz / fs * k) * np.hamming(n)
    return h / h.sum()


def bandpass_taps(lo_hz: float, hi_hz: float, fs: float, transition_hz: float) -> np.ndarray:
    """Real band-pass: low-pass prototype shifted to +-centre, unity at centre."""
    h = lowpass_taps((hi_hz - lo_hz) / 2, fs, transition_hz)
    k = np.arange(h.size) - (h.size - 1) / 2
    fc = (lo_hz + hi_hz) / 2
    g = 2.0 * h * np.cos(2 * np.pi * fc / fs * k)
    return g / abs(np.sum(g * np.exp(-2j * np.pi * fc / fs * k)))


def complex_bandpass_taps(lo_hz: float, hi_hz: float, fs: float, transition_hz: float):
    """One-sided (analytic) band-pass lo..hi: rejects the mirror band, which
    is what makes SSB pitch exact and the opposite sideband vanish."""
    h = lowpass_taps((hi_hz - lo_hz) / 2, fs, transition_hz)
    k = np.arange(h.size) - (h.size - 1) / 2
    return h * np.exp(2j * np.pi * (lo_hz + hi_hz) / 2 / fs * k)


# -- streaming primitives ----------------------------------------------------


class _Fir:
    """Non-decimating streaming FIR (history + block, 'valid' convolution)."""

    def __init__(self, taps: np.ndarray, dtype=np.float32) -> None:
        self._dtype = dtype
        self._h = np.asarray(taps).astype(dtype)
        self.reset()

    def reset(self) -> None:
        self._hist = np.zeros(self._h.size - 1, dtype=self._dtype)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.zeros(0, dtype=self._dtype)
        buf = np.concatenate([self._hist, x.astype(self._dtype, copy=False)])
        if self._hist.size:
            self._hist = buf[buf.size - self._hist.size :]
        return np.convolve(buf, self._h, mode="valid").astype(self._dtype, copy=False)


class _Decimator:
    """Streaming decimate-by-D FIR computing only the kept outputs (the
    rds._DecimFIR matvec idea, but dtype-clean for real taps). Output m
    depends on input up to absolute index m*D + D-1 -- ChannelExtractor's
    de-rotation relies on that."""

    def __init__(self, taps: np.ndarray, decim: int, dtype=np.complex64) -> None:
        d = int(decim)
        k = -(-len(taps) // d)
        padded = np.zeros(k * d, dtype=np.asarray(taps).dtype)
        padded[: len(taps)] = taps
        self._d, self._k, self._dtype = d, k, dtype
        self._h = padded.reshape(k, d)[:, ::-1].astype(dtype)
        self.reset()

    def reset(self) -> None:
        self._hist = np.zeros((self._k - 1, self._d), dtype=self._dtype)
        self._rem = np.zeros(0, dtype=self._dtype)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._rem, x.astype(self._dtype, copy=False)])
        rows = x.size // self._d
        self._rem = x[rows * self._d :]
        if rows == 0:
            return np.zeros(0, dtype=self._dtype)
        xh = np.concatenate([self._hist, x[: rows * self._d].reshape(rows, self._d)])
        k = self._k
        y = xh[k - 1 : k - 1 + rows] @ self._h[0]
        for j in range(1, k):
            y += xh[k - 1 - j : k - 1 - j + rows] @ self._h[j]
        if k > 1:
            self._hist = xh[rows:]
        return y


class _Resampler:
    """Streaming polyphase rational resampler (up/down). The prototype is
    designed at fs_in*up; each output gathers K inputs and one phase row,
    so cost is K MACs per *output* sample whatever up/down are."""

    def __init__(self, up: int, down: int, taps: np.ndarray, dtype=np.complex64) -> None:
        self._up, self._down, self._dtype = int(up), int(down), dtype
        k = -(-len(taps) // self._up)
        padded = np.zeros(k * self._up)
        padded[: len(taps)] = taps * self._up  # zero-stuffing loses 1/up of the gain
        self._k = k
        self._hp = padded.reshape(k, self._up).T.astype(np.float32)  # [phase, j]
        self._offs = (k - 1) - np.arange(k)
        self.reset()

    def reset(self) -> None:
        self._hist = np.zeros(self._k - 1, dtype=self._dtype)
        self._t = 0  # next output's position, in 1/up input samples, from block start

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.size
        xb = np.concatenate([self._hist, x.astype(self._dtype, copy=False)])
        if self._k > 1:
            self._hist = xb[xb.size - (self._k - 1) :]
        total = n * self._up
        if self._t >= total:
            self._t -= total
            return np.zeros(0, dtype=self._dtype)
        count = (total - self._t - 1) // self._down + 1
        ts = self._t + self._down * np.arange(count, dtype=np.int64)
        self._t = int(self._t + count * self._down - total)
        base, phase = np.divmod(ts, self._up)
        taps = xb[base[:, None] + self._offs[None, :]]
        return np.einsum("ij,ij->i", taps, self._hp[phase]).astype(self._dtype, copy=False)


class _OnePole:
    """y[n] = a*y[n-1] + b*x[n], vectorised: within a chunk the recursion has
    the closed form y[k] = a^k (a*y0 + sum_{i<=k} b x[i] a^-i). Chunks are
    short enough that a^-L stays <= e^18, so float64 keeps ~1e-8 precision."""

    def __init__(self, a: float, b: float, *, prime: bool = False) -> None:
        self._a, self._b, self._prime = float(a), float(b), prime
        span = int(18.0 / max(-np.log(self._a), 1e-12)) if self._a > 0 else 1
        self._pow = self._a ** np.arange(min(max(span, 1), 65536), dtype=np.float64)
        self.reset()

    @classmethod
    def lowpass_tau(cls, tau_s: float, fs: float, **kw) -> "_OnePole":
        a = float(np.exp(-1.0 / (fs * tau_s)))
        return cls(a, 1.0 - a, **kw)

    def reset(self) -> None:
        self._y: float | None = None if self._prime else 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        out = np.empty(x.size, dtype=np.float64)
        if x.size == 0:
            return out
        y = float(x[0]) if self._y is None else self._y
        step = self._pow.size
        for s in range(0, x.size, step):
            seg = x[s : s + step].astype(np.float64) * self._b
            pw = self._pow[: seg.size]
            yk = pw * (self._a * y + np.cumsum(seg / pw))
            out[s : s + seg.size] = yk
            y = float(yk[-1])
        self._y = y
        return out


def _ramp(prev: float, values: np.ndarray, starts: np.ndarray, n: int) -> np.ndarray:
    """Per-chunk gains -> per-sample curve, ramping linearly from the previous
    chunk's gain so gain changes never click."""
    ends = np.append(starts[1:], n) - 1
    return np.interp(np.arange(n), np.concatenate([[-1], ends]), np.concatenate([[prev], values]))


class _Agc:
    """Peak AGC evaluated per 5 ms chunk (a per-sample loop would be Python-
    slow), fast or slow attack and slow decay, gain ramped across chunks."""

    def __init__(self, fs: float, attack_s: float, decay_s: float, target: float = 0.3) -> None:
        self._chunk = max(1, int(fs * 0.005))
        self._att = 1.0 - np.exp(-self._chunk / (fs * attack_s))
        self._dec = float(np.exp(-self._chunk / (fs * decay_s)))
        self._target = target
        self._max_gain = 1e5
        self.reset()

    def reset(self) -> None:
        self._env: float | None = None
        self._gain = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x.astype(np.float32)
        starts = np.arange(0, x.size, self._chunk)
        peaks = np.maximum.reduceat(np.abs(x), starts)
        gains = np.empty(peaks.size)
        env = self._env
        for i, pk in enumerate(peaks):
            if env is None:
                env = float(pk)
            elif pk > env:
                env += self._att * (pk - env)
            else:
                env = self._dec * env + (1.0 - self._dec) * pk
            gains[i] = min(self._target / max(env, 1e-12), self._max_gain)
        self._env = env
        curve = _ramp(self._gain, gains, starts, x.size)
        self._gain = float(gains[-1])
        return (x * curve).astype(np.float32)


# -- decimation plan -----------------------------------------------------------


@dataclass(frozen=True)
class DecimationPlan:
    fs_in: float
    stages: tuple[int, ...]  # integer decimation factors, first (largest) at fs_in
    up: int  # then resample by up/down to reach out_rate exactly
    down: int

    @property
    def mid_rate(self) -> float:
        return self.fs_in / int(np.prod(self.stages, dtype=np.int64))


def _factor(d: int) -> tuple[int, ...] | None:
    """Fewest factors in 2..10 (at most three), largest first: the big
    decimation runs at the full rate where the wide transition band makes
    its filter cheap."""
    if d == 1:
        return ()
    for n in range(1, _MAX_STAGES + 1):
        found = _factor_n(d, n, _MAX_STAGE_DECIM)
        if found:
            return found
    return None


def _factor_n(d: int, n: int, cap: int) -> tuple[int, ...] | None:
    if n == 1:
        return (d,) if 2 <= d <= cap else None
    for f in range(min(cap, d), 1, -1):
        if d % f == 0:
            rest = _factor_n(d // f, n - 1, f)
            if rest:
                return (f, *rest)
    return None


def decimation_plan(fs_in: float, out_rate: int) -> DecimationPlan:
    """Integer stages to a rate near out_rate, then an exact rational step.

    2/8/10 MS/s and 250 kS/s -> 48 kHz go through 50 kHz (stages 10x4,
    10x8x2, 10x10x2, 5; then 24/25); -> 250 kHz is integer (8, 8x4, 10x4).
    Among decimations within 20 % of the ratio that factor into stages <=10,
    the largest whose resampling numerator is <=32 wins: the rational step
    then runs at the lowest rate with a short polyphase table.
    """
    fs = int(round(fs_in))
    ratio = fs / out_rate
    hi = max(1, int(ratio))
    lo = max(1, int(np.ceil(0.8 * ratio)))
    best = None
    for d in range(hi, lo - 1, -1):
        stages = _factor(d)
        if stages is None:
            continue
        frac = Fraction(int(out_rate) * d, fs)
        key = (frac.numerator > 32, -d)
        if best is None or key < best[0]:
            best = (key, stages, frac)
    if best is None:
        best = (None, (), Fraction(int(out_rate), fs))
    _, stages, frac = best
    if frac.numerator > _MAX_RESAMPLE_UP:
        raise ValueError(f"no resampling plan from {fs} Hz to {out_rate} Hz")
    plan = DecimationPlan(float(fs), tuple(stages), frac.numerator, frac.denominator)
    assert all(2 <= s <= _MAX_STAGE_DECIM for s in plan.stages)
    assert Fraction(fs, int(np.prod(plan.stages, dtype=np.int64))) * frac == out_rate
    return plan


# -- channel extraction ----------------------------------------------------------


def _channel_tw(cutoff_hz: float) -> float:
    return max(0.25 * cutoff_hz, 500.0)


def channel_filter(demodulator: str, bandwidth_hz: float) -> tuple[float, float, int]:
    """(cutoff, protected band, out_rate) the extractor uses for a mode."""
    mode = demodulator.upper()
    if mode == "WFM":
        return _WFM_CUTOFF_HZ, _WFM_PROTECT_HZ, MPX_RATE_HZ
    if mode in ("USB", "LSB"):
        cutoff = bandwidth_hz + _SSB_GUARD_HZ
    elif mode == "CW":
        cutoff = bandwidth_hz / 2 + _SSB_GUARD_HZ
    else:
        cutoff = bandwidth_hz / 2
    return cutoff, cutoff, VOICE_RATE_HZ


def filter_margin_hz(demodulator: str, bandwidth_hz: int) -> int:
    """How far beyond +-bandwidth/2 the extractor actually reaches (cutoff
    plus half its transition band): ChannelDescriptor.fits() uses this so a
    channel is only accepted when everything the filter passes was recorded."""
    cutoff, _, _ = channel_filter(demodulator, bandwidth_hz)
    return max(0, int(np.ceil(cutoff + _channel_tw(cutoff) / 2 - bandwidth_hz / 2)))


class ChannelExtractor:
    """Shift the channel at ``offset_hz`` to DC and bring it to ``out_rate``.

    The shift is folded into the first stage (band-pass taps = low-pass
    modulated to the channel, de-rotated at the decimated rate, the
    rds.WfmChannel trick) so no full-rate NCO is needed; with no integer
    stage a plain NCO does it. Phase is carried across blocks either way.
    """

    def __init__(
        self,
        fs_in: float,
        offset_hz: float,
        out_rate: int,
        cutoff_hz: float | None = None,
        protect_hz: float | None = None,
    ) -> None:
        self.fs_in = float(fs_in)
        self.offset_hz = float(offset_hz)
        self.out_rate = int(out_rate)
        self.plan = decimation_plan(fs_in, out_rate)
        mid = self.plan.mid_rate
        nyq = 0.5 * min(mid, self.out_rate)
        cutoff = 0.45 * self.out_rate if cutoff_hz is None else float(cutoff_hz)
        tw = _channel_tw(cutoff)
        self.cutoff_hz = min(cutoff, nyq - tw / 2)
        self._protect = min(protect_hz or self.cutoff_hz, 0.45 * mid)
        self._tw = tw
        self._w0 = 2 * np.pi * self.offset_hz / self.fs_in
        self.reset()

    def reset(self) -> None:
        self._stages: list[_Decimator] = []
        rate = self.fs_in
        for i, d in enumerate(self.plan.stages):
            r_out = rate / d
            # Only [-protect, protect] must survive alias-free; everything
            # between it and r_out - protect is removed by later stages.
            h = lowpass_taps(r_out / 2, rate, max(r_out - 2 * self._protect, 0.1 * r_out))
            if i == 0:
                h = h * np.exp(1j * self._w0 * np.arange(h.size))
            self._stages.append(_Decimator(h, d))
            rate = r_out
        d1 = self.plan.stages[0] if self.plan.stages else 1
        self._rot_step = (self._w0 * d1) % (2 * np.pi)
        self._rot_phase = (self._w0 * (d1 - 1)) % (2 * np.pi) if self.plan.stages else 0.0
        self._final: _Resampler | _Fir | None = None
        up, down = self.plan.up, self.plan.down
        if (up, down) != (1, 1):
            h = lowpass_taps(self.cutoff_hz, rate * up, self._tw)
            self._final = _Resampler(up, down, h)
        elif self.cutoff_hz < 0.4 * self.out_rate:  # else the stages already shape it
            self._final = _Fir(lowpass_taps(self.cutoff_hz, rate, self._tw), np.complex64)

    def _rotate(self, y: np.ndarray) -> np.ndarray:
        if y.size == 0 or self._w0 == 0.0:
            return y
        ph = self._rot_phase + self._rot_step * np.arange(y.size)
        self._rot_phase = float((self._rot_phase + self._rot_step * y.size) % (2 * np.pi))
        return y * np.exp(-1j * ph).astype(np.complex64)

    def process(self, x: np.ndarray) -> np.ndarray:
        y = np.asarray(x, dtype=np.complex64)
        if self._stages:
            y = self._rotate(self._stages[0].process(y))
            for st in self._stages[1:]:
                y = st.process(y)
        else:
            y = self._rotate(y)
        if self._final is not None:
            y = self._final.process(y)
        return y


# -- demodulators ------------------------------------------------------------------


class _Listening:
    """Volume/mute shared by the demods; listening path only (§2)."""

    volume: float = 1.0
    muted: bool = False

    def _out(self, x: np.ndarray) -> np.ndarray:
        if self.muted:
            return np.zeros(x.size, dtype=np.float32)
        return (x * self.volume).astype(np.float32)


class _Discriminator:
    def __init__(self, scale: float) -> None:
        self._scale = np.float32(scale)
        self.reset()

    def reset(self) -> None:
        self._prev = np.complex64(0)

    def process(self, z: np.ndarray) -> np.ndarray:
        if z.size == 0:
            return np.zeros(0, dtype=np.float32)
        zz = np.concatenate([[self._prev], z.astype(np.complex64, copy=False)])
        self._prev = zz[-1]
        return np.angle(zz[1:] * np.conj(zz[:-1])).astype(np.float32) * self._scale


_SQ_NOISE_HP_HZ = 5_000.0  # voice lives below ~3 kHz; above this is discriminator noise
_SQ_CHUNK_S = 0.010
_SQ_HYSTERESIS_DB = 3.0


@lru_cache(maxsize=32)
def _noise_only_db(bw_hz: float, fs: int) -> float:
    """Discriminator high-band power for noise alone in this channel: the 0 dB
    reference the squelch threshold is measured against. Deterministic
    (seeded) so the same settings always squelch the same way."""
    rng = np.random.default_rng(0)
    n = fs // 5
    w = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64)
    cutoff = bw_hz / 2
    ch = np.convolve(w, lowpass_taps(cutoff, fs, _channel_tw(cutoff)), mode="valid")
    disc = _Discriminator(fs / (2 * np.pi * NFM_DEVIATION_HZ)).process(ch)
    hp = np.convolve(disc, _squelch_hp(fs), mode="valid")
    return float(10 * np.log10(np.mean(hp.astype(np.float64) ** 2) + 1e-20))


def _squelch_hp(fs: int) -> np.ndarray:
    lp = lowpass_taps(_SQ_NOISE_HP_HZ, fs, 2_500.0)
    hp = -lp
    hp[lp.size // 2] += 1.0
    return hp


class NfmDemod(_Listening):
    """Quadrature discriminator feeding two independent paths (§2):

    - ``.flat``: discriminator output as-is (only the channel filter shaped
      it) -- what decoders get; squelch/volume/de-emphasis never touch it;
    - the return value: 750 us de-emphasis -> 300-3000 Hz band-pass ->
      noise squelch -> volume/mute, for the ear only.

    Squelch: ``squelch_db`` is the required *quieting* -- how far the
    discriminator's >5 kHz noise must fall below its noise-only level
    (``_noise_only_db``) to open; it closes again 3 dB lower (hysteresis).
    ``None`` disables it (always open).
    """

    def __init__(self, bw_hz: float = 12_500, fs: int = VOICE_RATE_HZ) -> None:
        self.bw_hz = float(bw_hz)
        self.fs = int(fs)
        self.squelch_db: float | None = None
        self.audio_mode = "voice"
        self._ref_db = _noise_only_db(self.bw_hz, self.fs)
        self._chunk = max(1, int(self.fs * _SQ_CHUNK_S))
        self.reset()

    def reset(self) -> None:
        self._disc = _Discriminator(self.fs / (2 * np.pi * NFM_DEVIATION_HZ))
        self._deemph = _OnePole.lowpass_tau(750e-6, self.fs)
        self._bp = _Fir(bandpass_taps(300.0, 3_000.0, self.fs, 600.0))
        self._hp = _Fir(_squelch_hp(self.fs))
        self._noise_p: float | None = None
        self.squelch_open = self.squelch_db is None
        self._gate = 1.0 if self.squelch_open else 0.0
        self.quieting_db: float | None = None
        self.flat = np.zeros(0, dtype=np.float32)

    def process(self, z: np.ndarray) -> np.ndarray:
        flat = self._disc.process(z)
        self.flat = flat
        if flat.size == 0:
            return flat
        audio = flat if self.audio_mode == "flat" else self._bp.process(self._deemph.process(flat))
        audio = audio * self._squelch(flat)
        return self._out(audio)

    def _squelch(self, flat: np.ndarray) -> np.ndarray:
        noise = self._hp.process(flat).astype(np.float64) ** 2
        starts = np.arange(0, noise.size, self._chunk)
        sums = np.add.reduceat(noise, starts)
        lens = np.diff(np.append(starts, noise.size))
        gates = np.empty(starts.size)
        p = self._noise_p
        for i, pw in enumerate(sums / lens):
            p = pw if p is None else p + 0.3 * (pw - p)
            q = self._ref_db - 10 * np.log10(p + 1e-20)
            self.quieting_db = float(q)
            if self.squelch_db is None:
                self.squelch_open = True
            elif self.squelch_open and q < self.squelch_db - _SQ_HYSTERESIS_DB:
                self.squelch_open = False
            elif not self.squelch_open and q >= self.squelch_db:
                self.squelch_open = True
            gates[i] = 1.0 if self.squelch_open else 0.0
        self._noise_p = p
        curve = _ramp(self._gate, gates, starts, noise.size)
        self._gate = float(gates[-1])
        return curve


class AmDemod(_Listening):
    """|z| -> DC block (one-pole, 0.5 Hz) -> slow AGC -> 5 kHz low-pass."""

    def __init__(self, bw_hz: float = 10_000, fs: int = VOICE_RATE_HZ) -> None:
        self.bw_hz = float(bw_hz)
        self.fs = int(fs)
        self.agc_mode = "default"
        self.manual_gain = 1.0
        self.reset()

    def reset(self) -> None:
        # Primed with the first envelope sample so the carrier's DC doesn't
        # arrive as a 0.3 s step that the AGC would then chase.
        self._dc = _OnePole.lowpass_tau(1 / (2 * np.pi * 0.5), self.fs, prime=True)
        attack, decay = (0.005, 0.3) if self.agc_mode == "fast" else (0.1, 1.0)
        self._agc = _Agc(self.fs, attack_s=attack, decay_s=decay)
        self._lp = _Fir(lowpass_taps(5_000.0, self.fs, 2_000.0))

    def process(self, z: np.ndarray) -> np.ndarray:
        env = np.abs(z).astype(np.float64)
        if env.size == 0:
            return np.zeros(0, dtype=np.float32)
        audio = (env - self._dc.process(env)).astype(np.float32)
        gained = (
            audio * self.manual_gain
            if self.agc_mode == "off"
            else self._agc.process(audio)
        )
        return self._out(self._lp.process(gained))


class SsbDemod(_Listening):
    """Analytic band-pass over the wanted sideband, real part, AGC.

    USB keeps +0..+bw (a carrier at +1 kHz -> exactly 1 kHz); LSB conjugates
    first, mirroring -bw..0 onto it. The filter is one-sided, so the other
    sideband is ~53 dB down -- that is what "pitch is exact" rests on.
    """

    def __init__(
        self, side: str = "USB", bw_hz: float = 2_700, fs: int = VOICE_RATE_HZ, *, agc=True
    ) -> None:
        side = side.upper()
        if side not in ("USB", "LSB"):
            raise ValueError(f"SSB side must be USB or LSB, not {side!r}")
        self.side, self.bw_hz, self.fs, self.agc = side, float(bw_hz), int(fs), agc
        self.agc_mode = "default"
        self.manual_gain = 1.0
        self._band = (0.0, self.bw_hz)
        self._shift_hz = 0.0
        self.reset()

    def reset(self) -> None:
        lo, hi = self._band
        self._bp = _Fir(complex_bandpass_taps(lo, hi, self.fs, 400.0), np.complex64)
        attack, decay = (0.1, 1.0) if self.agc_mode == "slow" else (0.005, 0.5)
        self._agc = _Agc(self.fs, attack_s=attack, decay_s=decay)
        self._phase = 0.0

    def set_passband(self, low_hz: float, high_hz: float) -> None:
        self._band = (float(low_hz), float(high_hz))
        self.reset()

    def _shift(self, z: np.ndarray) -> np.ndarray:
        if self._shift_hz == 0.0 or z.size == 0:
            return z
        w = 2 * np.pi * self._shift_hz / self.fs
        ph = self._phase + w * np.arange(z.size)
        self._phase = float((self._phase + w * z.size) % (2 * np.pi))
        return z * np.exp(1j * ph).astype(np.complex64)

    def process(self, z: np.ndarray) -> np.ndarray:
        z = np.asarray(z, dtype=np.complex64)
        if self.side == "LSB":
            z = np.conj(z)
        y = self._bp.process(self._shift(z)).real.astype(np.float32)
        gained = (
            y * self.manual_gain
            if self.agc_mode == "off" or not self.agc
            else self._agc.process(y)
        )
        return self._out(gained)


class CwDemod(SsbDemod):
    """SSB with the channel-centre carrier shifted up to ``pitch_hz`` and a
    ``bw_hz`` band-pass around it: a keyed carrier -> pitch-Hz tone."""

    DEFAULT_PITCH_HZ = 700.0

    def __init__(
        self,
        bw_hz: float = 500,
        pitch_hz: float = DEFAULT_PITCH_HZ,
        fs: int = VOICE_RATE_HZ,
        *,
        agc=True,
    ) -> None:
        self.side, self.bw_hz, self.fs, self.agc = "USB", float(bw_hz), int(fs), agc
        self.agc_mode = "default"
        self.manual_gain = 1.0
        self.pitch_hz = float(pitch_hz)
        self._band = (max(0.0, pitch_hz - bw_hz / 2), pitch_hz + bw_hz / 2)
        self._shift_hz = self.pitch_hz
        self.reset()

    def reset(self) -> None:
        super().reset()
        # Keying edges need a fast AGC so the gain is right on the first dit.
        if self.agc_mode in ("default", "fast"):
            self._agc = _Agc(self.fs, attack_s=0.002, decay_s=0.3)

    def set_pitch(self, pitch_hz: float) -> None:
        self.pitch_hz = float(pitch_hz)
        self._shift_hz = self.pitch_hz
        self._band = (
            max(0.0, self.pitch_hz - self.bw_hz / 2),
            self.pitch_hz + self.bw_hz / 2,
        )
        self.reset()


class WfmDemod(_Listening):
    """Discriminator at 250 kHz (rds.WfmChannel's scaling: 1.0 == 75 kHz)
    -> ``.mpx`` for RdsDemod; mono audio: 15 kHz low-pass (also the pilot
    and RDS killer) decimating to 50 kHz -> 24/25 -> 48 kHz -> 75 us
    de-emphasis. Stereo is out of S3; a pilotless station is just mono."""

    def __init__(self, fs: int = MPX_RATE_HZ, out_rate: int = VOICE_RATE_HZ) -> None:
        self.fs, self.out_rate = int(fs), int(out_rate)
        self.deemphasis_us = 75
        self._plan = decimation_plan(self.fs, self.out_rate)
        self.reset()

    def reset(self) -> None:
        self._disc = _Discriminator(self.fs / (2 * np.pi * WFM_DEVIATION_HZ))
        rate = float(self.fs)
        self._stages = []
        for i, d in enumerate(self._plan.stages):
            if i == 0:
                h = lowpass_taps(15_000.0, rate, 7_000.0)
            else:
                h = lowpass_taps(rate / d / 2, rate, rate / d - 2 * 16_000.0)
            self._stages.append(_Decimator(h, d, np.float32))
            rate /= d
        up, down = self._plan.up, self._plan.down
        self._rs = None
        if (up, down) != (1, 1):
            self._rs = _Resampler(up, down, lowpass_taps(16_000.0, rate * up, 8_000.0), np.float32)
        self._deemph = _OnePole.lowpass_tau(max(self.deemphasis_us, 1) * 1e-6, self.out_rate)
        self.mpx = np.zeros(0, dtype=np.float32)

    def process(self, z: np.ndarray) -> np.ndarray:
        self.mpx = self._disc.process(z)
        y = self.mpx
        for st in self._stages:
            y = st.process(y)
        if self._rs is not None:
            y = self._rs.process(y)
        if self.deemphasis_us:
            y = self._deemph.process(y)
        return self._out(y.astype(np.float32))


# -- complete chain ------------------------------------------------------------------


class DemodChain:
    """ChannelExtractor + one demodulator (+ RDS for WFM) for one channel.
    ``process(iq)`` returns listening audio at 48 kHz; ``flat`` (NFM) holds
    the same block's decoder tap."""

    def __init__(
        self, fs_in: float, offset_hz: float, demodulator: str, bandwidth_hz: float
    ) -> None:
        mode = demodulator.upper()
        if mode not in DEMODULATORS:
            raise ValueError(f"unknown demodulator {demodulator!r}; use one of {DEMODULATORS}")
        self.mode = mode
        cutoff, protect, out_rate = channel_filter(mode, bandwidth_hz)
        self.extractor = ChannelExtractor(fs_in, offset_hz, out_rate, cutoff, protect)
        self.rds_demod: RdsDemod | None = None
        self.rds_parser: RdsParser | None = None
        if mode == "NFM":
            self.demod = NfmDemod(bandwidth_hz)
        elif mode == "AM":
            self.demod = AmDemod(bandwidth_hz)
        elif mode in ("USB", "LSB"):
            self.demod = SsbDemod(mode, bandwidth_hz)
        elif mode == "CW":
            self.demod = CwDemod(bandwidth_hz)
        else:
            self.demod = WfmDemod(out_rate)
            self.rds_demod = RdsDemod(out_rate)
            self.rds_parser = RdsParser()
        self.selected = np.zeros(0, dtype=np.complex64)

    @property
    def flat(self) -> np.ndarray | None:
        return self.demod.flat if isinstance(self.demod, NfmDemod) else None

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.selected = self.extractor.process(iq)
        audio = self.demod.process(self.selected)
        if self.rds_demod is not None:
            for group in self.rds_demod.process(self.demod.mpx):
                self.rds_parser.feed(group)
            self.rds_parser.add_block_stats(*self.rds_demod.pop_block_stats())
        return audio
