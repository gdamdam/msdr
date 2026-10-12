"""features.py: classification on synthetic 25 kHz channels
-- pure tone, AM, 2-FSK at two bauds, OOK, and voice_score
on synthesized discriminator audio."""

import numpy as np

from msdr.survey.families import family_for
from msdr.survey.features import (
    CANDIDATES,
    classify_modulation,
    crossing_rate_hz,
    envelope_cv,
    ifreq_bimodality,
    ifreq_std_hz,
    keying_timing,
    occupied_bw_hz,
    snap_symbol_rate,
    spectral_symmetry,
    symbol_rate_hz,
    voice_score,
)
from tests.helpers import gmsk

_RATE = 25_000.0
_N = int(_RATE * 0.5)  # 0.5 s channel


def _features(x: np.ndarray) -> dict:
    return {
        "env_cv": envelope_cv(x),
        "ifreq_std_hz": ifreq_std_hz(x, _RATE),
        "occ_bw_hz": occupied_bw_hz(x, _RATE),
        "symmetry": spectral_symmetry(x),
    }


def _tone(freq_hz: float) -> np.ndarray:
    t = np.arange(_N) / _RATE
    return np.exp(1j * 2 * np.pi * freq_hz * t).astype(np.complex128)


def _am(carrier_hz: float, audio_hz: float, depth: float) -> np.ndarray:
    t = np.arange(_N) / _RATE
    envelope = 1 + depth * np.cos(2 * np.pi * audio_hz * t)
    return envelope * np.exp(1j * 2 * np.pi * carrier_hz * t)


def _fsk(baud: float, dev_hz: float, seed: int = 1) -> np.ndarray:
    """2-FSK with symbol boundaries on the exact (real-valued) symbol grid,
    quantized per-boundary rather than to a fixed integer symbol length --
    the latter accumulates rounding error into a baud-rate bias at high
    bauds relative to a 25 kHz channel (e.g. 4800 baud is ~5.2 samples/
    symbol)."""
    rng = np.random.default_rng(seed)
    n_syms = int(_N / (_RATE / baud)) + 2
    bits = rng.integers(0, 2, n_syms)
    boundaries = np.round(np.arange(n_syms + 1) * (_RATE / baud)).astype(int)
    freq_per_sample = np.zeros(boundaries[-1], dtype=np.float64)
    for i in range(n_syms):
        freq_per_sample[boundaries[i] : boundaries[i + 1]] = dev_hz if bits[i] else -dev_hz
    freq_per_sample = freq_per_sample[:_N]
    phase = 2 * np.pi * np.cumsum(freq_per_sample) / _RATE
    return np.exp(1j * phase).astype(np.complex128)


def _ook(baud: float, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n_syms = int(_N / (_RATE / baud)) + 2
    bits = rng.integers(0, 2, n_syms)
    boundaries = np.round(np.arange(n_syms + 1) * (_RATE / baud)).astype(int)
    env = np.zeros(boundaries[-1])
    for i in range(n_syms):
        env[boundaries[i] : boundaries[i + 1]] = 1.0 if bits[i] else 0.05
    env = env[:_N]
    t = np.arange(_N) / _RATE
    return (env * np.exp(1j * 2 * np.pi * 30 * t)).astype(np.complex128)


def test_pure_tone_classified_as_carrier():
    f = _features(_tone(100.0))
    result = classify_modulation(f)
    assert result["kind"] == "carrier"
    assert result["confidence"] >= 0.6


def test_am_tone_classified_as_am():
    f = _features(_am(50.0, 400.0, 0.5))
    result = classify_modulation(f)
    assert result["kind"] == "am"
    assert result["confidence"] >= 0.6


def test_fsk_1200_baud_classified_and_rate_within_3pct():
    x = _fsk(1200.0, 2400.0)
    f = _features(x)
    result = classify_modulation(f)
    assert result["kind"] == "fm_fsk"
    assert result["confidence"] >= 0.6

    raw, strength = symbol_rate_hz(x, _RATE)
    assert raw is not None
    assert strength > 0.0
    assert abs(raw - 1200.0) / 1200.0 <= 0.03
    assert snap_symbol_rate(raw) == 1200.0


def test_fsk_4800_baud_classified_and_rate_within_3pct():
    x = _fsk(4800.0, 2400.0)
    f = _features(x)
    result = classify_modulation(f)
    assert result["kind"] == "fm_fsk"
    assert result["confidence"] >= 0.6

    raw, strength = symbol_rate_hz(x, _RATE)
    assert raw is not None
    assert strength > 0.0
    assert abs(raw - 4800.0) / 4800.0 <= 0.03
    assert snap_symbol_rate(raw) == 4800.0


def test_ook_20_baud_classified_as_ook():
    f = _features(_ook(20.0))
    result = classify_modulation(f)
    assert result["kind"] == "ook"
    assert result["confidence"] >= 0.6


def test_symbol_rate_hz_on_noise_returns_none():
    # Pure noise: no clock line anywhere, so the peak-to-median ratio never
    # clears _LINE_STRENGTH_GATE -- must not report a rate (this is exactly
    # the field bug: a fixed dwell on silence/noise reported 16666.67 Hz,
    # the search's minimum-lag artifact, as if it were a real signal).
    rng = np.random.default_rng(7)
    noise = (rng.normal(0.0, 1.0, _N) + 1j * rng.normal(0.0, 1.0, _N)).astype(np.complex128)
    raw, strength = symbol_rate_hz(noise, _RATE)
    assert raw is None
    assert strength < 3.0


def test_symbol_rate_hz_never_exceeds_9600_baud():
    # A signal well above the 9600 baud search ceiling either reports
    # nothing or clips into range -- never a rate above 9600.
    x = _fsk(19200.0, 4800.0, seed=8)
    raw, _strength = symbol_rate_hz(x, _RATE)
    assert raw is None or raw <= 9600.0


def test_snap_symbol_rate_within_tolerance():
    for candidate in CANDIDATES:
        assert snap_symbol_rate(candidate * 1.02) == candidate
        assert snap_symbol_rate(candidate * 0.98) == candidate


def test_snap_symbol_rate_outside_tolerance_and_none():
    assert snap_symbol_rate(700.0) is None  # nowhere near 600 or 1200 (>3%)
    assert snap_symbol_rate(None) is None
    assert snap_symbol_rate(0.0) is None


# -- voice_score -----------------------------------------------------

_AUDIO_RATE = 22050
_AUDIO_N = _AUDIO_RATE  # 1 s


def _bandlimited_noise(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, _AUDIO_N)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(_AUDIO_N, d=1.0 / _AUDIO_RATE)
    spec[~((freqs >= 300.0) & (freqs <= 3000.0))] = 0.0
    out = np.fft.irfft(spec, _AUDIO_N)
    return out / np.max(np.abs(out))


def test_voice_score_speech_like_signal_is_voice():
    t = np.arange(_AUDIO_N) / _AUDIO_RATE
    speech_like = _bandlimited_noise(3) * (1 + 0.8 * np.cos(2 * np.pi * 4.0 * t))
    speech_like = 0.5 * speech_like / np.max(np.abs(speech_like))
    result = voice_score(speech_like, rate=_AUDIO_RATE)
    assert result["kind"] == "voice"
    assert result["score"] > 0.6


def test_voice_score_fsk_audio_is_data():
    # A steady-envelope data tone with no syllabic-rate structure -- a random
    # phase walk (like a real recovered FSK data tone) rather than a clean
    # sinusoid, so it isn't mistaken for a single spectral line either.
    rng = np.random.default_rng(4)
    t = np.arange(_AUDIO_N) / _AUDIO_RATE
    fsk_audio = 0.5 * np.sign(
        np.sin(2 * np.pi * 1200.0 * t + np.cumsum(rng.integers(0, 2, _AUDIO_N) * 0.01))
    )
    result = voice_score(fsk_audio, rate=_AUDIO_RATE)
    assert result["kind"] == "data"
    assert result["score"] < 0.35


def test_voice_score_near_silence_is_silence():
    rng = np.random.default_rng(5)
    silence = rng.normal(0.0, 1e-4, _AUDIO_N)
    result = voice_score(silence, rate=_AUDIO_RATE)
    assert result["kind"] == "silence"
    assert result["score"] == 0.0


def _fsk4(baud: float, dev_hz: float, seed: int = 6) -> np.ndarray:
    """4-level FSK (DMR/P25/NXDN-style) at `baud` symbols/s -- same symbol-
    grid construction as `_fsk` but with 4 evenly-spaced levels instead of 2,
    matching real 4FSK's +-1/+-3 deviation steps."""
    rng = np.random.default_rng(seed)
    n_syms = int(_N / (_RATE / baud)) + 2
    levels = rng.choice([-3.0, -1.0, 1.0, 3.0], size=n_syms)
    boundaries = np.round(np.arange(n_syms + 1) * (_RATE / baud)).astype(int)
    freq_per_sample = np.zeros(boundaries[-1], dtype=np.float64)
    step = dev_hz / 3.0
    for i in range(n_syms):
        freq_per_sample[boundaries[i] : boundaries[i + 1]] = levels[i] * step
    freq_per_sample = freq_per_sample[:_N]
    phase = 2 * np.pi * np.cumsum(freq_per_sample) / _RATE
    return np.exp(1j * phase).astype(np.complex128)


def test_dmr_4fsk_4800_baud_voice_score_is_data():
    # Sanity check: a 4800-baud 4FSK signal is DMR/P25/NXDN
    # digital voice -- it must never be reported as "voice" without an
    # actual decode, since its discriminator audio has no speech structure.
    x = _fsk4(4800.0, 1944.0)
    audio = (np.angle(x[1:] * np.conj(x[:-1])) * _RATE / (2 * np.pi)).astype(np.float64)
    audio /= 5000.0  # same +-5 kHz deviation normalisation the analyzer uses
    result = voice_score(audio, rate=_RATE)
    assert result["kind"] == "data"


def test_noisy_carrier_is_still_a_carrier():
    # Receiver noise makes a real carrier's instantaneous frequency wander by
    # hundreds of Hz; occupied bandwidth still shows it's unmodulated.
    rng = np.random.default_rng(4)
    n = 25_000
    t = np.arange(n) / 25_000.0
    x = np.exp(1j * 2 * np.pi * 300 * t) + 0.15 * (rng.normal(size=n) + 1j * rng.normal(size=n))
    feats = {
        "env_cv": envelope_cv(x),
        "ifreq_std_hz": ifreq_std_hz(x, 25_000.0),
        "occ_bw_hz": occupied_bw_hz(x, 25_000.0),
    }
    assert feats["ifreq_std_hz"] > 50.0  # the old rule's blind spot
    assert classify_modulation(feats)["kind"] == "carrier"


def test_bell202_score_spots_afsk_and_not_noise():
    from msdr.survey.features import bell202_score

    rate = 25_000.0
    rng = np.random.default_rng(5)
    n = int(rate)  # 1 s
    # 1200-baud mark/space AFSK audio, phase-continuous
    bits = rng.integers(0, 2, size=1200)
    freq = np.repeat(np.where(bits == 1, 1200.0, 2200.0), int(rate / 1200) + 1)[:n]
    afsk = np.sin(2 * np.pi * np.cumsum(freq) / rate) + 0.1 * rng.normal(size=n)
    assert bell202_score(afsk, rate) > 4.0
    assert bell202_score(rng.normal(size=n), rate) < 2.0
    voice_like = np.sin(2 * np.pi * 440 * np.arange(n) / rate) * (
        1 + np.sin(2 * np.pi * 4 * np.arange(n) / rate)
    )
    assert bell202_score(voice_like + 0.1 * rng.normal(size=n), rate) < 2.0


def test_occupied_bw_counts_thin_fm_sidebands_not_just_the_carrier():
    # NOAA-like: low-index FM, the carrier line holds about half the power.
    # The old -20 dB-from-peak width called this ~5 Hz wide (a "carrier").
    rng = np.random.default_rng(6)
    n = int(_RATE * 3)
    t = np.arange(n) / _RATE
    audio = np.sin(2 * np.pi * 700 * t) + 0.6 * np.sin(2 * np.pi * 1900 * t)
    phase = 2 * np.pi * 1200 * np.cumsum(audio) / _RATE  # ~1.2 kHz deviation
    noise = 0.02 * (rng.normal(size=n) + 1j * rng.normal(size=n))
    fm = np.exp(1j * phase) + noise
    carrier = np.exp(1j * 2 * np.pi * 150 * t) + noise
    assert occupied_bw_hz(fm, _RATE) > 2000.0
    assert occupied_bw_hz(carrier, _RATE) < 200.0


# -- GMSK 9600 is 2-level FSK, not PSK -----------------------------------


def _awgn(x: np.ndarray, snr_db: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    return x + n * np.sqrt(10 ** (-snr_db / 10) / 2)


def _gmsk9600(snr_db: float, seed: int = 21) -> np.ndarray:
    rng = np.random.default_rng(seed)
    bits = list(rng.integers(0, 2, int(0.5 * gmsk.BAUD) + 50))
    x = gmsk.gmsk_iq(gmsk.nrzi(bits), _RATE, 0.3)[:_N]
    return _awgn(x, snr_db, seed)


def _psk(order: int, baud: float, snr_db: float, seed: int = 22) -> np.ndarray:
    """Band-limited BPSK/QPSK: windowed-sinc pulses (+-6 symbols)."""
    rng = np.random.default_rng(seed)
    n_sym = int(_N * baud / _RATE) + 12
    offset = np.pi / 4 if order == 4 else 0.0
    symbols = np.exp(1j * (2 * np.pi * rng.integers(0, order, n_sym) / order + offset))
    t = np.arange(_N) * baud / _RATE
    x = np.zeros(_N, dtype=complex)
    for k in range(n_sym):
        d = t - k
        m = np.abs(d) < 6
        x[m] += symbols[k] * np.sinc(d[m]) * (0.5 + 0.5 * np.cos(np.pi * d[m] / 6))
    x /= np.sqrt(np.mean(np.abs(x) ** 2))
    return _awgn(x, snr_db, seed)


def _s4_features(x: np.ndarray) -> dict:
    raw, strength = symbol_rate_hz(x, _RATE)
    _, fit = crossing_rate_hz(x, _RATE)
    return {
        **_features(x),
        "fsk_line_strength": strength if raw is not None else 0.0,
        "ifreq_bimodality": ifreq_bimodality(x, _RATE),
        "crossing_fit": fit,
    }


def test_noisy_gmsk_9600_is_fm_fsk_not_psk_like_with_9600_family():
    x = _gmsk9600(7.0)
    f = _s4_features(x)
    # The noise pushes the envelope into the old psk_like window, and the
    # clock-line search finds nothing: exactly the AIS failure.
    assert 0.2 <= f["env_cv"] <= 0.5
    assert symbol_rate_hz(x, _RATE)[0] is None
    assert (
        classify_modulation(
            {k: v for k, v in f.items() if k not in ("ifreq_bimodality", "crossing_fit")}
        )["kind"]
        == "psk_like"
    )

    assert classify_modulation(f)["kind"] == "fm_fsk"
    rate, fit = crossing_rate_hz(x, _RATE)
    assert rate is not None and abs(rate - 9600.0) / 9600.0 <= 0.03
    assert snap_symbol_rate(rate) == 9600.0
    family = family_for("fm_fsk", snap_symbol_rate(rate), "nfm", f["occ_bw_hz"])
    assert family["name"] == "GMSK/FSK 9600 (AIS or packet)"
    assert family["suggested_decoders"] == ["ais", "packet9600"]


def test_clean_gmsk_9600_rate_from_crossings():
    rate, fit = crossing_rate_hz(_gmsk9600(30.0), _RATE)
    assert fit > 0.8
    assert snap_symbol_rate(rate) == 9600.0


def test_bpsk_and_qpsk_stay_psk_like():
    for order, snr in ((2, 10.0), (2, 20.0), (4, 10.0)):
        x = _psk(order, 4800.0, snr)
        f = _s4_features(x)
        assert f["ifreq_bimodality"] < 0.45, (order, snr)
        assert f["crossing_fit"] < 0.3, (order, snr)
        assert classify_modulation(f)["kind"] == "psk_like", (order, snr)


def test_noise_has_no_crossing_rate():
    rate, _ = crossing_rate_hz(_awgn(np.zeros(_N, dtype=complex), 0.0, 23), _RATE)
    assert rate is None


def test_keying_timing_accepts_dots_and_dashes_only():
    rng = np.random.default_rng(24)
    unit = 0.06
    morse = [unit * (1 if rng.random() < 0.5 else 3) * rng.uniform(0.9, 1.1) for _ in range(20)]
    morse += [unit, 3 * unit]  # both element kinds present
    fit = keying_timing(morse)
    assert fit["fits_cw"] is True
    assert abs(fit["unit_s"] - unit) / unit < 0.15

    beacon = list(rng.uniform(0.4, 0.6, 12))  # one on-length: a keyed carrier, not Morse
    assert keying_timing(beacon)["fits_cw"] is False
    random_runs = list(rng.uniform(0.01, 1.0, 20))
    assert keying_timing(random_runs)["fits_cw"] is False
    assert keying_timing([0.06, 0.18])["fits_cw"] is None
