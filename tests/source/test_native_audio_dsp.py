"""Deterministic listening-path DSP checks using generated audio and IQ."""

import numpy as np
import pytest

from msdr.source.audio_dsp import NativeAudioEffects
from msdr.source.dsp import WFM_DEVIATION_HZ, AmDemod, CwDemod, NfmDemod, SsbDemod, WfmDemod

FS = 48_000


def _tone(freq: float, n: int, fs: int = FS, amplitude: float = 0.2, start: int = 0):
    t = (np.arange(n) + start) / fs
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _tone_rms(x: np.ndarray, freq: float, fs: int = FS) -> float:
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    t = np.arange(x.size) / fs
    # Least-squares projection is robust to spectral leakage and stateful delay.
    basis = np.column_stack((np.sin(2 * np.pi * freq * t), np.cos(2 * np.pi * freq * t)))
    coeff, *_ = np.linalg.lstsq(basis, x, rcond=None)
    return float(np.linalg.norm(coeff) / np.sqrt(2))


def _tone_residual_power(x: np.ndarray, freq: float, fs: int = FS) -> float:
    x = np.asarray(x, dtype=np.float64)
    t = np.arange(x.size) / fs
    basis = np.column_stack(
        (np.sin(2 * np.pi * freq * t), np.cos(2 * np.pi * freq * t), np.ones(x.size))
    )
    coeff, *_ = np.linalg.lstsq(basis, x, rcond=None)
    residual = x - basis @ coeff
    return float(np.mean(residual**2))


def _effect_output(settings: dict, blocks: list[np.ndarray]) -> np.ndarray:
    effect = NativeAudioEffects(FS)
    effect.configure(settings)
    return np.concatenate([effect.process(block) for block in blocks])


def test_manual_notch_attenuates_target_and_bypass_restores_tone():
    x = _tone(1_000, FS * 2)
    dry = _effect_output({}, [x])
    wet = _effect_output(
        {"notch_enabled": True, "notch_frequency_hz": 1_000, "notch_width_hz": 100}, [x]
    )
    dry_level = _tone_rms(dry[FS // 2 :], 1_000)
    wet_level = _tone_rms(wet[FS // 2 :], 1_000)
    assert 20 * np.log10(wet_level / dry_level) < -20

    bypassed = _effect_output(
        {"notch_enabled": False, "notch_frequency_hz": 1_000, "notch_width_hz": 100}, [x]
    )
    assert _tone_rms(bypassed[FS // 2 :], 1_000) == pytest.approx(dry_level, rel=0.01)

    effect = NativeAudioEffects(FS)
    effect.configure({"notch_enabled": True, "notch_frequency_hz": 1_000, "notch_width_hz": 100})
    effect.process(x)
    effect.configure({"notch_enabled": False, "notch_frequency_hz": 1_000, "notch_width_hz": 100})
    after_bypass = effect.process(_tone(1_000, FS, start=2 * FS))
    assert _tone_rms(after_bypass[FS // 2 :], 1_000) == pytest.approx(dry_level, rel=0.02)
    assert np.max(np.abs(np.diff(after_bypass))) < 0.2


def test_notch_state_is_continuous_and_independent_of_input_blocking():
    x = _tone(1_000, FS * 2) + _tone(3_200, FS * 2, amplitude=0.05)
    settings = {"notch_enabled": True, "notch_frequency_hz": 1_000, "notch_width_hz": 120}
    whole = _effect_output(settings, [x])
    chunked = _effect_output(settings, [x[i : i + 137] for i in range(0, x.size, 137)])
    assert whole.size == chunked.size == x.size
    np.testing.assert_allclose(chunked, whole, atol=4e-4, rtol=4e-4)
    assert np.isfinite(chunked).all()
    assert np.max(np.abs(np.diff(chunked))) < 0.2


def test_noise_reduction_improves_interference_snr_and_preserves_clean_tone():
    n = FS * 4
    tone = _tone(1_000, n, amplitude=0.08)
    rng = np.random.default_rng(293)
    noisy = tone + rng.normal(0, 0.07, n).astype(np.float32)
    clean_out = _effect_output({"nr_enabled": True, "nr_strength": 0.6}, [tone])
    noisy_out = _effect_output({"nr_enabled": True, "nr_strength": 0.6}, [noisy])
    # Ignore startup and measure signal power against residual error/noise.
    clean_tail = clean_out[FS:]
    noisy_tail = noisy_out[FS:]
    clean_gain = _tone_rms(clean_tail, 1_000) / _tone_rms(tone[FS:], 1_000)
    before = _tone_rms(tone[FS:], 1_000) ** 2 / np.mean((noisy[FS:] - tone[FS:]) ** 2)
    after = _tone_rms(noisy_tail, 1_000) ** 2 / _tone_residual_power(noisy_tail, 1_000)
    assert 0.65 <= clean_gain <= 1.3
    assert 10 * np.log10(after / before) > 1.0


def test_noise_reduction_stream_result_is_frame_size_independent():
    n = FS * 2
    t = np.arange(n) / FS
    rng = np.random.default_rng(71)
    x = (0.1 * np.sin(2 * np.pi * 900 * t) + rng.normal(0, 0.04, n)).astype(np.float32)
    settings = {"nr_enabled": True, "nr_strength": 0.7}
    whole = _effect_output(settings, [x])
    chunked = _effect_output(settings, [x[i : i + 733] for i in range(0, n, 733)])
    assert whole.size == chunked.size == n
    np.testing.assert_allclose(chunked, whole, atol=2e-5, rtol=2e-5)
    assert np.isfinite(chunked).all()


def test_blanker_reduces_isolated_impulses_and_leaves_clean_tone_alone():
    clean = _tone(1_200, FS, amplitude=0.15)
    impulse = clean.copy()
    impulse[FS // 2] = 8.0
    settings = {"blanker_enabled": True, "blanker_threshold": 8.0}
    cleaned = _effect_output(settings, [impulse])
    assert abs(cleaned[FS // 2]) < 0.3
    assert np.max(np.abs(cleaned)) < np.max(np.abs(impulse)) / 10
    clean_processed = _effect_output(settings, [clean])
    np.testing.assert_allclose(clean_processed, clean, atol=1e-6, rtol=1e-6)


def _am_input(amplitude: float, n: int, start: int = 0) -> np.ndarray:
    t = (np.arange(n) + start) / FS
    envelope = 0.4 + amplitude * np.cos(2 * np.pi * 1_000 * t)
    return envelope.astype(np.complex64)


def test_agc_off_and_manual_gain_are_linear_and_fast_slow_track_attack():
    low = _am_input(0.04, FS)
    high = _am_input(0.12, FS)
    off = AmDemod(fs=FS)
    off.agc_mode = "off"
    off.manual_gain = 2.0
    off.reset()
    y_low = off.process(low)
    y_high = off.process(high)
    low_level = _tone_rms(y_low[FS // 4 :], 1_000)
    high_level = _tone_rms(y_high[FS // 4 :], 1_000)
    assert high_level / low_level == pytest.approx(3.0, rel=0.12)

    fast = AmDemod(fs=FS)
    fast.agc_mode = "fast"
    fast.reset()
    slow = AmDemod(fs=FS)
    slow.agc_mode = "slow"
    slow.reset()
    step = np.concatenate((_am_input(0.025, FS // 2), _am_input(0.2, FS, FS // 2)))
    fast_out = fast.process(step)
    slow_out = slow.process(step)
    attack = slice(FS // 2 + FS // 100, FS // 2 + FS // 10)
    fast_level = _tone_rms(fast_out[attack], 1_000)
    slow_level = _tone_rms(slow_out[attack], 1_000)
    assert fast_level < slow_level * 0.8


def test_ssb_passband_edges_reject_low_tone_and_cw_pitch_moves_audio():
    n = FS // 2
    t = np.arange(n) / FS
    iq = (0.2 * np.exp(2j * np.pi * 300 * t) + 0.2 * np.exp(2j * np.pi * 1800 * t)).astype(
        np.complex64
    )
    ssb = SsbDemod("USB", 2700, agc=False)
    wide = ssb.process(iq)
    ssb.set_passband(1000, 2700)
    narrowed = ssb.process(iq)
    tail = slice(FS // 10, None)
    assert _tone_rms(narrowed[tail], 300) < _tone_rms(wide[tail], 300) * 0.05
    assert _tone_rms(narrowed[tail], 1800) > _tone_rms(wide[tail], 1800) * 0.8

    carrier = np.full(n, 0.2 + 0j, dtype=np.complex64)
    cw = CwDemod(500, agc=False)
    cw.set_pitch(900)
    output = cw.process(carrier)
    assert _tone_rms(output[tail], 900) > 0.05
    assert _tone_rms(output[tail], 700) < 0.01


def test_nfm_flat_listening_bypasses_voice_filter_without_changing_decoder_tap():
    n = FS // 2
    t = np.arange(n) / FS
    phase = np.cumsum(2 * np.pi * 5000 * (0.2 * np.sin(2 * np.pi * 5000 * t)) / FS)
    iq = np.exp(1j * phase).astype(np.complex64)
    voice = NfmDemod()
    voice_audio = voice.process(iq)
    flat = NfmDemod()
    flat.audio_mode = "flat"
    flat_audio = flat.process(iq)
    np.testing.assert_allclose(flat.flat, voice.flat, atol=1e-6)
    np.testing.assert_allclose(flat_audio, flat.flat, atol=1e-6)
    assert _tone_rms(voice_audio[FS // 10 :], 5000) < _tone_rms(flat_audio[FS // 10 :], 5000) * 0.05


def _wfm_audio(deemphasis_us: int) -> np.ndarray:
    fs = 250_000
    n = fs * 3
    t = np.arange(n) / fs
    mod = 0.08 * np.sin(2 * np.pi * 1_000 * t) + 0.08 * np.sin(2 * np.pi * 5_000 * t)
    phase = np.cumsum(2 * np.pi * WFM_DEVIATION_HZ * mod / fs)
    iq = np.exp(1j * phase).astype(np.complex64)
    demod = WfmDemod(fs=fs)
    demod.deemphasis_us = deemphasis_us
    demod.reset()
    blocks = [demod.process(iq[i : i + 997]) for i in range(0, iq.size, 997)]
    return np.concatenate(blocks)


def test_wfm_deemphasis_options_have_expected_frequency_response():
    levels = {}
    for tau in (0, 50, 75):
        audio = _wfm_audio(tau)
        tail = audio[FS:]
        assert np.isfinite(tail).all()
        levels[tau] = _tone_rms(tail, 5_000) / _tone_rms(tail, 1_000)
    assert levels[0] == pytest.approx(1.0, rel=0.18)
    assert levels[50] < levels[0] * 0.85
    assert levels[75] < levels[50] * 0.9
