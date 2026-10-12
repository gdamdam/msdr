"""IQ feature extraction + classification: pure numpy over one
channel's complex64 samples (25 kHz nominal, the channelizer's `out_rate`).
No IQ I/O, no asyncio -- this module is the part of the analyzer that runs
inside `asyncio.to_thread`, mirroring how `channelizer.py` keeps the numpy
math separate from the pump loop.
"""

import numpy as np

# Known symbol-rate candidates in baud (spec "Facts"): POCSAG/AFSK, FLEX,
# DMR/P25/NXDN, packet9600, RTTY/NAVTEX, CW. snap_symbol_rate() only ever
# returns one of these (or None); classify.py/family_for() key off the
# resulting value, not the raw estimate.
CANDIDATES = (
    45.45,
    50.0,
    75.0,
    100.0,
    300.0,
    600.0,
    1200.0,
    1600.0,
    2400.0,
    3200.0,
    4800.0,
    9600.0,
    19200.0,
)

# Below this, an "active" sample is indistinguishable from the noise floor
# between OOK/burst symbols -- including it would swamp ifreq_std_hz with
# the noise floor's essentially-random phase.
_ACTIVE_ENV_FRACTION = 0.3


def envelope_cv(x: np.ndarray) -> float:
    """std/mean of |x|: near 0 for constant-envelope FM/FSK, high for OOK."""
    env = np.abs(x)
    mean = float(env.mean())
    if mean <= 0.0:
        return 0.0
    return float(env.std()) / mean


def ifreq_std_hz(x: np.ndarray, rate: float) -> float:
    """std of instantaneous frequency (rad/sample -> Hz) over active samples.

    A constant offset shifts every sample of ifreq by the same amount, so std
    is insensitive to residual mixing error -- only the *spread* matters,
    which is what separates a steady carrier/AM tone (near 0) from FSK
    toggling between two tones (large) or noisy PSK-like phase (also large).
    """
    if x.size < 2:
        return 0.0
    ifreq = np.angle(x[1:] * np.conj(x[:-1])) * rate / (2 * np.pi)
    env = np.abs(x[1:])
    peak = float(env.max()) if env.size else 0.0
    if peak <= 0.0:
        return 0.0
    active = env > _ACTIVE_ENV_FRACTION * peak
    if not np.any(active):
        return 0.0
    return float(np.std(ifreq[active]))


def occupied_bw_hz(x: np.ndarray, rate: float, fraction: float = 0.99) -> float:
    """Occupied bandwidth: the span holding `fraction` of the signal's power
    above the noise (ITU-style 99% bandwidth), from a Welch PSD at ~50 Hz
    resolution with the noise floor (10th-percentile bin) subtracted.

    Replaces a "-20 dB from the peak" width on a single full-length FFT:
    at that ~0.3 Hz resolution an FM signal's carrier line holds all its
    power in one bin while the voice sidebands are spread thin over kHz, so
    everything but the carrier read >20 dB down -- NOAA weather radio (FM
    voice, strong carrier) measured 5 Hz wide and was called a bare carrier
    (live 2026-09-26). A 99% power width counts the sidebands however thin.
    Channels are baseband-centred, so the PSD is fftshift-ed."""
    n = x.size
    seg = 512 if n >= 2048 else max(8, 1 << int(np.log2(max(8, n // 4))))
    if n < seg:
        return 0.0
    segs = n // seg
    frames = np.asarray(x[: segs * seg], dtype=np.complex128).reshape(segs, seg)
    window = np.hanning(seg)
    psd = np.mean(np.abs(np.fft.fft(frames * window, axis=1)) ** 2, axis=0)
    psd = np.fft.fftshift(psd)
    # Noise reference from the passband only: the channel filter's roll-off
    # bins at the edges sit far below the in-band noise and made a 10th
    # percentile over all bins read ~25 dB low (every noise bin "signal").
    freqs = np.fft.fftshift(np.fft.fftfreq(seg, d=1.0 / rate))
    passband = np.abs(freqs) < 0.36 * rate
    floor = np.percentile(psd[passband], 20.0)
    # Only bins clearly above the noise count: ~150 averaged Welch segments
    # keep noise bins within ~1.5x the floor, but summed over 512 bins their
    # residual still outweighed 1% of a bare carrier's power and stretched
    # its 99% width across the whole channel.
    excess = np.where(psd > 2.0 * floor, psd - floor, 0.0)
    total = float(excess.sum())
    if total <= 0.0:
        return 0.0
    cum = np.cumsum(excess) / total
    tail = (1.0 - fraction) / 2.0
    lo = int(np.searchsorted(cum, tail))
    hi = int(np.searchsorted(cum, 1.0 - tail))
    return float(hi - lo + 1) * rate / seg


def spectral_symmetry(x: np.ndarray, rate: float | None = None) -> float:
    """|P(+f) - P(-f)| / (P(+f) + P(-f)): ~1 for SSB (energy on one side of
    the carrier only), ~0 for AM/FM (symmetric sidebands). The residual
    offset (DC bias from imperfect fine-mixing) is removed first so it
    doesn't itself register as one-sided energy at f=0."""
    n = x.size
    if n < 8:
        return 0.0
    centered = np.asarray(x, dtype=np.complex128) - np.mean(x)
    spec = np.fft.fftshift(np.fft.fft(centered))
    psd = np.abs(spec) ** 2
    mid = n // 2
    pos = float(psd[mid + 1 :].sum())
    neg = float(psd[:mid].sum())
    total = pos + neg
    if total <= 0.0:
        return 0.0
    return abs(pos - neg) / total


_MIN_BAUD = 40.0  # just under the slowest candidate (45.45)
# Live VHF/UHF land-mobile traffic (spec "Facts") tops out at 9600 baud
# (FSK9600/P25 C4FM); a clock search that also looks for 19200 baud finds
# only noise on this band -- the minimum lag it implies (~1.3 samples at a
# 25 kHz channel rate) is below the autocorrelation's own useful resolution,
# so the "peak" it reports there is a noise artifact, not a real line (this
# is what produced the field's degenerate 16666.67 Hz readings). Capping the
# search at 9600 baud (>= 2.6 samples/symbol at 25 kHz) keeps it in the
# regime where the lag estimate actually means something.
_MAX_BAUD = 9600.0
# A clock line has to stand this many times above the search segment's
# median to count as real; below it, it's noise-floor wobble that happened
# to have a local maximum somewhere in the lag range.
_LINE_STRENGTH_GATE = 3.0


def _strongest_line(signal: np.ndarray, rate: float) -> tuple[float | None, float, float]:
    """Symbol-clock line via the autocorrelation of `signal` (Wiener-Khinchin:
    equivalent information to a spectral line, but reading it off in the lag
    domain finds the *fundamental* directly instead of whichever harmonic
    happens to be tallest -- a sparse, data-driven pulse train (FSK
    transitions, an OOK envelope) has harmonics of comparable height, so a
    plain FFT argmax over the whole spectrum picks one of those at random.
    Sub-sample parabolic interpolation around the peak lag matters because
    the fastest candidate (9600 baud) is only a few samples/symbol at a
    25 kHz channel rate -- integer-lag resolution alone is >3% off.

    Returns (rate_hz, peak_height, line_strength): `peak_height` is the raw
    (0-1) normalised autocorrelation height at the line, used by
    symbol_rate_hz to pick the stronger of the FSK/envelope searches the way
    it always has; `line_strength` is that same peak's peak-to-median ratio
    over the searched lag range -- a real symbol clock stands well above the
    segment's typical (median) value regardless of how strong its absolute
    autocorrelation happens to be, where noise-floor wobble does not, so
    that ratio (not the height) is what the caller gates the *chosen* line
    on. `rate_hz` is returned unconditionally here (not yet gated/clipped --
    see symbol_rate_hz) since which line even is the chosen one isn't known
    until both searches return."""
    m = signal.size
    if m < 32:
        return None, 0.0, 0.0
    detrended = signal - float(np.mean(signal))
    n_fft = 1
    while n_fft < 2 * m:
        n_fft *= 2
    spec = np.fft.rfft(detrended, n_fft)
    acorr = np.fft.irfft(spec * np.conj(spec), n_fft)[:m]
    if acorr[0] <= 0.0:
        return None, 0.0, 0.0
    acorr = acorr / acorr[0]

    min_lag = max(1, int(rate / _MAX_BAUD))
    # Skip the zero-lag main lobe: it's the channel filter's own correlation
    # of neighbouring samples, not a clock. At short lags it produced a strong
    # false "line" on everything -- voice, even a bare carrier read ~9600 baud
    # with line strength 68-81 (live 2026-09-26).
    below = np.flatnonzero(acorr[1:] <= 0.0)
    if below.size:
        min_lag = max(min_lag, int(below[0]) + 1)
    max_lag = min(m - 2, int(rate / _MIN_BAUD))
    if max_lag <= min_lag:
        return None, 0.0, 0.0
    segment = acorr[min_lag : max_lag + 1]
    # First local maximum (smallest lag) is the fundamental; harmonics of it
    # are later local maxima at integer multiples of the same lag. No
    # fallback to argmax when none clears 0.1: on pure noise, nothing ever
    # does (measured false-positive rate ~100% with an argmax fallback vs.
    # ~0-2% without one, across synthetic and channelized-noise trials) --
    # an argmax over an unstructured autocorrelation is just picking the
    # least-bad point in the search range, not a real clock line.
    local_maxima = [
        i
        for i in range(1, segment.size - 1)
        if segment[i] > segment[i - 1] and segment[i] > segment[i + 1] and segment[i] > 0.1
    ]
    if not local_maxima:
        return None, 0.0, 0.0
    lag = min_lag + local_maxima[0]

    y0, y1, y2 = acorr[lag - 1], acorr[lag], acorr[lag + 1]
    denom = y0 - 2 * y1 + y2
    delta = float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5)) if denom != 0.0 else 0.0
    if y1 <= 0.0:
        return None, 0.0, 0.0

    median = float(np.median(np.abs(segment)))
    ratio = (y1 / median) if median > 0.0 else float("inf")
    return rate / (lag + delta), float(y1), ratio


def symbol_rate_hz(x: np.ndarray, rate: float) -> tuple[float | None, float]:
    """Baud-rate estimate: the stronger of two clock-line searches --
    |delta inst-freq| (FSK: a pulse at every symbol transition, regardless of
    data content, since transitions are constrained to the symbol clock's
    grid) and the envelope itself (OOK/AM: amplitude toggles at the symbol
    rate) -- picked by raw peak height, same as ever. Returns
    (rate_hz, line_strength) only once the winning line's peak-to-median
    ratio clears _LINE_STRENGTH_GATE, else (None, line_strength) -- see
    _strongest_line. Never returns a rate outside [40, 9600] baud."""
    if x.size < 18:
        return None, 0.0
    ifreq = np.angle(x[1:] * np.conj(x[:-1])) * rate / (2 * np.pi)
    d_ifreq = np.abs(np.diff(ifreq))
    env = np.abs(x)

    fsk_rate, fsk_height, fsk_ratio = _strongest_line(d_ifreq, rate)
    env_rate, env_height, env_ratio = _strongest_line(env, rate)

    if fsk_height >= env_height:
        rate_hz, ratio = fsk_rate, fsk_ratio
    else:
        rate_hz, ratio = env_rate, env_ratio

    if rate_hz is None or ratio < _LINE_STRENGTH_GATE:
        return None, ratio
    return float(np.clip(rate_hz, _MIN_BAUD, _MAX_BAUD)), ratio


def snap_symbol_rate(raw: float | None) -> float | None:
    """Nearest of CANDIDATES within 3%, else None (caller falls back to the
    raw estimate -- see the Label contract's `symbol_rate_hz` comment)."""
    if raw is None or raw <= 0.0:
        return None
    best = min(CANDIDATES, key=lambda c: abs(c - raw))
    if abs(best - raw) / best <= 0.03:
        return best
    return None


def fsk_clock_line(x: np.ndarray, rate: float) -> tuple[float | None, float]:
    """Find a narrow high-rate symbol clock in frequency-transition energy.

    A shaped 4-FSK signal can have no useful autocorrelation peak even when
    its transitions repeat precisely on the symbol grid. Average half-second
    spectra of |delta instantaneous frequency| and compare each candidate's
    narrow peak with the surrounding 800 Hz. This is only *clock* evidence:
    the analyzer also requires a stepped instantaneous-frequency distribution
    before treating it as FSK or overriding a speech-like audio score.
    """
    seg = int(rate * 0.5)
    if seg < 256 or x.size < seg + 2:
        return None, 0.0
    ifreq = np.angle(x[1:] * np.conj(x[:-1]))
    transitions = np.abs(np.diff(ifreq))
    count = transitions.size // seg
    if not count:
        return None, 0.0
    frames = transitions[: count * seg].reshape(count, seg)
    frames = frames - frames.mean(axis=1, keepdims=True)
    psd = np.mean(np.abs(np.fft.rfft(frames * np.hanning(seg), axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(seg, d=1.0 / rate)
    best_rate, best_db = None, 0.0
    for candidate in CANDIDATES:
        if not 3200.0 <= candidate <= _MAX_BAUD or candidate >= 0.4 * rate:
            continue
        nearby = (freqs >= candidate - 400.0) & (freqs <= candidate + 400.0)
        peak_bins = (freqs >= candidate * 0.99) & (freqs <= candidate * 1.01)
        if not np.any(peak_bins):
            continue
        floor = float(np.median(psd[nearby]))
        if floor <= 0.0:
            continue
        peak_index = np.flatnonzero(peak_bins)[int(np.argmax(psd[peak_bins]))]
        db = float(10.0 * np.log10(float(psd[peak_index]) / floor))
        if db > best_db:
            best_rate, best_db = float(freqs[peak_index]), db
    return (best_rate if best_db >= 18.0 else None), best_db


# 2-level FSK/GMSK evidence. Sarle's bimodality coefficient of the
# 3-tap-smoothed instantaneous frequency: 1/3 for a Gaussian, 5/9 for a
# uniform, -> 1 for two point masses. Measured on synthetic 25 kHz channels
# (6 seeds each): GMSK 9600 >= 0.50 down to 7 dB SNR, BPSK/QPSK 4800 <= 0.26,
# noise <= 0.40, but QPSK 9600 reaches 0.46 -- so bimodality alone is not
# enough, and the crossing-grid fit below must agree.
BIMODAL_GATE = 0.45
# Crossing-grid fit: frequency zero-crossings of a 2-level FSK land on the
# symbol grid. GMSK 9600 >= 0.40 at 7 dB SNR; QPSK 9600 ~0.14; BPSK none.
_CROSSING_FIT_GATE = 0.3


def _smoothed_ifreq(x: np.ndarray, rate: float, *, active_only: bool) -> np.ndarray:
    """Instantaneous frequency (Hz), 3-tap smoothed, median removed.
    active_only keeps just the samples above ifreq_std_hz's envelope gate
    (for distribution statistics); crossing timing needs the contiguous
    series, since dropping samples would corrupt the intervals."""
    if x.size < 8:
        return np.zeros(0)
    ifreq = np.angle(x[1:] * np.conj(x[:-1])) * rate / (2 * np.pi)
    ifreq = np.convolve(ifreq, np.ones(3) / 3.0, mode="same")
    if active_only:
        env = np.abs(x[1:])
        peak = float(env.max())
        if peak <= 0.0:
            return np.zeros(0)
        ifreq = ifreq[env > _ACTIVE_ENV_FRACTION * peak]
    return ifreq - float(np.median(ifreq)) if ifreq.size else ifreq


def ifreq_bimodality(x: np.ndarray, rate: float) -> float:
    """Sarle's bimodality coefficient (skew^2 + 1) / kurtosis of the
    instantaneous frequency -- high when the signal sits on two frequencies
    (2-FSK, GMSK) rather than around one (PSK, AM, voice, noise)."""
    v = _smoothed_ifreq(x, rate, active_only=True)
    if v.size < 32:
        return 0.0
    std = float(v.std())
    if std <= 0.0:
        return 0.0
    z = v / std
    kurt = float(np.mean(z**4))
    return float((np.mean(z**3) ** 2 + 1.0) / kurt) if kurt > 0.0 else 0.0


def crossing_rate_hz(x: np.ndarray, rate: float) -> tuple[float | None, float]:
    """Symbol rate from the intervals between instantaneous-frequency zero
    crossings (sub-sample interpolated): a 2-level FSK only changes frequency
    on the symbol grid, so every interval is a whole number of symbols.

    Complements symbol_rate_hz's clock-line search, whose lag resolution is
    too coarse for GMSK 9600 at a 25 kHz channel (2.6 samples/symbol) and
    which found no line on it at all. Intervals are phase-free, so stitched
    bursts with different timing still agree. For each candidate rate
    <= _MAX_BAUD: round intervals to symbol counts (1-16, >= 80% coverage),
    least-squares the period, and score the residuals' circular
    concentration (1 = perfect grid, ~0 = none). The lowest candidate within
    80% of the best score wins (a 4800 Bd grid also fits 9600). Returns
    (rate_hz, fit), rate None below _CROSSING_FIT_GATE."""
    v = _smoothed_ifreq(x, rate, active_only=False)
    if v.size < 64:
        return None, 0.0
    neg = np.signbit(v)
    k = np.flatnonzero(neg[1:] != neg[:-1])
    if k.size < 16:
        return None, 0.0
    t = (k + v[k] / (v[k] - v[k + 1])) / rate
    d = np.diff(t)
    scores: list[tuple[float, float, float]] = []  # (candidate, fit, refined rate)
    for cand in CANDIDATES:
        if cand > _MAX_BAUD:
            continue
        n = np.round(d * cand)
        m = (n >= 1) & (n <= 16)
        if m.mean() < 0.8:
            continue
        period = float(np.sum(n[m] * d[m]) / np.sum(n[m] ** 2))
        resid = d[m] / period - n[m]
        fit = float(np.abs(np.mean(np.exp(2j * np.pi * resid))))
        scores.append((cand, fit, 1.0 / period))
    if not scores:
        return None, 0.0
    best_fit = max(fit for _, fit, _ in scores)
    _, fit, est = next(sc for sc in scores if sc[1] >= 0.8 * best_fit)
    if fit < _CROSSING_FIT_GATE:
        return None, fit
    return float(est), fit


def keying_timing(on_runs_s: list[float]) -> dict:
    """Do on-durations of a keyed carrier look like Morse? Dots are one
    unit, dashes three: split the sorted runs at their largest ratio gap and
    require two clusters each holding >= 20% of the runs, a gap ratio >= 1.8,
    long/short median ratio 2-4.5, each cluster tight (CV < 0.35), and at
    least 6 runs. Returns {"fits_cw", "unit_s", "runs"}
    -- fits_cw None (not False) when there are too few runs to tell."""
    runs = sorted(r for r in on_runs_s if r > 0.0)
    result: dict = {"fits_cw": None, "unit_s": None, "runs": len(runs)}
    if len(runs) < 6:
        return result
    arr = np.array(runs)
    ratios = arr[1:] / arr[:-1]
    cut = int(np.argmax(ratios)) + 1
    short, long_ = arr[:cut], arr[cut:]
    result["fits_cw"] = False
    # Both element kinds must be common (a lone outlier is not a cluster)
    # and clearly separated.
    if min(short.size, long_.size) < max(2, 0.2 * arr.size) or ratios[cut - 1] < 1.8:
        return result
    ratio = float(np.median(long_) / np.median(short))
    tight = all(float(c.std() / c.mean()) < 0.35 for c in (short, long_))
    if 2.0 <= ratio <= 4.5 and tight:
        result["fits_cw"] = True
        result["unit_s"] = float(np.median(short))
    return result


def classify_modulation(f: dict) -> dict:
    """Rules straight from the spec/plan: constant-envelope + wandering
    phase is FSK, wide envelope swing is OOK, moderate swing with a steady
    phase is AM, moderate envelope with wandering phase but no discrete FSK
    clock line is PSK-like, and a narrow, phase-steady signal is a bare
    carrier. `f` is the features dict (env_cv, ifreq_std_hz, occ_bw_hz) plus
    the optional `fsk_line_strength` the analyzer adds from symbol_rate_hz's
    second return value: 0.0 when no confirmed clock line was found (raw_rate
    None) or when no candidate call site has one -- e.g. testing this
    function directly on synthetic features -- else the line's peak-to-median
    ratio (>= _LINE_STRENGTH_GATE, i.e. >= 3.0), so the 0.15 threshold below
    just distinguishes "no confirmed line" from "confirmed". Optional
    `ifreq_bimodality` / `crossing_fit` (ifreq_bimodality() and
    crossing_rate_hz()) default to 0.0, i.e. no 2-level FSK evidence."""
    env_cv = float(f["env_cv"])
    ifreq_std = float(f["ifreq_std_hz"])
    occ_bw = float(f["occ_bw_hz"])
    fsk_line = float(f.get("fsk_line_strength", 0.0))

    # OOK is checked before the bare-carrier rule: a slow on/off envelope can
    # be just as spectrally narrow as an unmodulated tone (few, if any,
    # sidebands clear the occupied-bandwidth threshold), so occ_bw/ifreq_std
    # alone can't tell them apart -- only the envelope swing can.
    if env_cv > 0.8:
        return {"kind": "ook", "confidence": min(0.95, 0.6 + (env_cv - 0.8))}
    # A bare carrier is recognised by its occupied bandwidth and steady
    # envelope alone: ifreq_std is dominated by receiver noise on a real one
    # (live 2026-09-26: a 15 dB-SNR carrier at 144.000 read ifreq_std 464 Hz
    # and fell through to fm_fsk), while any real FM/FSK modulation spreads
    # well past 200 Hz.
    if occ_bw < 200.0 and env_cv < 0.25:
        return {"kind": "carrier", "confidence": 0.9}
    if env_cv < 0.25 and ifreq_std > 200.0:
        return {"kind": "fm_fsk", "confidence": 0.85}
    if 0.3 <= env_cv <= 0.8 and ifreq_std < 200.0:
        return {"kind": "am", "confidence": 0.75}
    # Two frequency levels switching on a symbol grid is 2-FSK/GMSK however
    # noisy the envelope reads (AIS/GMSK 9600 was reported psk_like, in the synthetic corpus
    # corpus): both the bimodality and the crossing-grid fit must agree --
    # never a frequency/allocation rule.
    if (
        env_cv <= 0.5
        and ifreq_std > 200.0
        and float(f.get("ifreq_bimodality", 0.0)) >= BIMODAL_GATE
        and float(f.get("crossing_fit", 0.0)) >= _CROSSING_FIT_GATE
    ):
        return {"kind": "fm_fsk", "confidence": 0.7}
    if 0.2 <= env_cv <= 0.5 and ifreq_std > 200.0 and fsk_line < 0.15:
        return {"kind": "psk_like", "confidence": 0.65}
    return {"kind": "unknown", "confidence": 0.3}


def _hilbert_envelope(audio: np.ndarray) -> np.ndarray:
    """Analytic-signal envelope via an FFT-domain Hilbert transform (no
    scipy): zero the negative-frequency half of the spectrum, double the
    positive half, keep DC/Nyquist as-is."""
    n = audio.size
    spec = np.fft.fft(audio)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1 : n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1 : (n + 1) // 2] = 2.0
    analytic = np.fft.ifft(spec * h)
    return np.abs(analytic)


def bell202_score(audio: np.ndarray, rate: float) -> float:
    """How clearly FM-discriminator audio carries Bell 202 AFSK (1200/2200 Hz
    mark/space: APRS, 1200-baud packet): the weaker of the two tones' power
    density over its neighbouring bands'. ~1 for noise or a carrier, ~5-7 on a
    real APRS burst (~7, live 2026-09-26). AFSK's data clock lives in the audio,
    so the RF symbol-rate search can't see it, and stitched packet bursts
    look syllabic to voice_score -- this catches it first."""
    audio = np.asarray(audio, dtype=np.float64)
    if audio.size < int(rate * 0.1):
        return 0.0
    spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size))) ** 2
    freqs = np.fft.rfftfreq(audio.size, d=1.0 / rate)

    def density(lo: float, hi: float) -> float:
        band = spec[(freqs >= lo) & (freqs <= hi)]
        return float(band.mean()) if band.size else 0.0

    mark_ref = density(700.0, 1000.0)
    space_ref = (density(1500.0, 1900.0) + density(2500.0, 2900.0)) / 2.0
    if mark_ref <= 0.0 or space_ref <= 0.0:
        return 0.0
    # +-100 Hz tone windows: wide enough for keyed (spread) tones, narrow
    # enough that FM noise's smooth slope can't fake a peak.
    return min(density(1100.0, 1300.0) / mark_ref, density(2100.0, 2300.0) / space_ref)


def voice_score(audio: np.ndarray, rate: float = 22050) -> dict:
    """Voice vs. data vs. silence on NFM-discriminator audio.

    Three ratios feed the score: syllabic envelope modulation (3-8 Hz, the
    rate speech amplitude naturally varies at), formant-band energy
    (300-3000 Hz), and spectral flux variance (speech's frame-to-frame
    spectral change is far less uniform than a data tone's). This is a
    speech-likeness heuristic, not proof of analog voice: bursty digital
    signals can score highly too. The analyzer must preserve contrary
    symbol evidence and expose speech-like results as tentative.
    """
    audio = np.asarray(audio, dtype=np.float64)
    n = audio.size
    if n == 0:
        return {"kind": "silence", "confidence": 0.9, "score": 0.0}

    rms = float(np.sqrt(np.mean(audio**2)))
    dbfs = 20.0 * np.log10(rms) if rms > 0.0 else -120.0
    if dbfs < -45.0:
        return {"kind": "silence", "confidence": 0.9, "score": 0.0}

    window = np.hanning(n)

    env = _hilbert_envelope(audio)
    env = env - env.mean()
    env_spec = np.abs(np.fft.rfft(env * window)) ** 2
    env_freqs = np.fft.rfftfreq(n, d=1.0 / rate)
    env_total = float(env_spec.sum())
    syllabic_ratio = (
        float(env_spec[(env_freqs >= 3.0) & (env_freqs <= 8.0)].sum()) / env_total
        if env_total > 0.0
        else 0.0
    )

    spec = np.abs(np.fft.rfft(audio * window)) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / rate)
    total = float(spec.sum())
    band_ratio = (
        float(spec[(freqs >= 300.0) & (freqs <= 3000.0)].sum()) / total if total > 0.0 else 0.0
    )

    frame_len = max(int(rate * 0.02), 32)  # 20 ms analysis frames for flux
    n_frames = n // frame_len
    flux_values = []
    prev_spec = None
    for i in range(n_frames):
        seg = audio[i * frame_len : (i + 1) * frame_len]
        seg_spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
        seg_spec = seg_spec / (np.linalg.norm(seg_spec) + 1e-12)  # normalise level out of flux
        if prev_spec is not None:
            flux_values.append(float(np.sum((seg_spec - prev_spec) ** 2)))
        prev_spec = seg_spec
    flux_var = float(np.var(flux_values)) if flux_values else 0.0

    score = float(np.clip(0.55 * syllabic_ratio + 0.3 * band_ratio + 40.0 * flux_var, 0.0, 1.0))

    if score > 0.6:
        kind = "voice"
        confidence = min(0.95, 0.5 + (score - 0.6))
    elif score < 0.35:
        kind = "data"
        confidence = min(0.95, 0.5 + (0.35 - score))
    else:
        kind = "data"  # ambiguous middle: never overclaim voice without a clear score
        confidence = 0.4

    return {"kind": kind, "confidence": confidence, "score": score}
