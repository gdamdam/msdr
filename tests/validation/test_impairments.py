"""Basic properties of msdr.validation.impairments (synthetic signals only)."""

from __future__ import annotations

import numpy as np
import pytest

from msdr.validation import impairments as imp

RATE = 48_000.0


def _tone(freq_hz: float = 1_000.0, n: int = 4800) -> np.ndarray:
    t = np.arange(n) / RATE
    return (0.5 * np.exp(2j * np.pi * freq_hz * t)).astype(np.complex64)


def _peak_hz(iq: np.ndarray) -> float:
    spec = np.fft.fftshift(np.fft.fft(iq * np.hanning(len(iq))))
    freqs = np.fft.fftshift(np.fft.fftfreq(len(iq), 1 / RATE))
    return float(freqs[np.argmax(np.abs(spec))])


def test_frequency_offset_shifts_spectral_peak():
    assert _peak_hz(imp.frequency_offset(_tone(), 3_000.0, RATE)) == pytest.approx(4_000, abs=20)


def test_off_centre_moves_peak_by_fraction_of_rate():
    assert _peak_hz(imp.off_centre(_tone(0.0), RATE, 0.25)) == pytest.approx(12_000, abs=20)


def test_sample_clock_error_stretches_time_axis():
    out = imp.sample_clock_error(_tone(), 10_000.0)  # 1% fast -> tone appears 1% higher
    assert len(out) == 4800
    assert _peak_hz(out[:4700]) == pytest.approx(1_010, abs=25)


def test_dc_spike_adds_mean():
    out = imp.dc_spike(_tone(), 0.2)
    assert np.mean(out) == pytest.approx(0.2 + 0.2j, abs=0.01)


def test_iq_swap_is_j_times_conjugate():
    x = _tone()
    np.testing.assert_allclose(imp.iq_swap(x), 1j * np.conj(x), atol=1e-6)
    assert _peak_hz(imp.iq_swap(x)) == pytest.approx(-1_000, abs=20)


def test_iq_imbalance_unit_is_identity_and_gain_applies():
    x = _tone()
    np.testing.assert_allclose(imp.iq_imbalance(x, 0.0, 0.0), x, atol=1e-6)
    out = imp.iq_imbalance(x, 6.0, 0.0)
    assert np.std(out.imag) / np.std(x.imag) == pytest.approx(10 ** (6 / 20), rel=0.01)


def test_drop_blocks_zeroes_every_nth_block_and_keeps_input():
    x = np.ones(2048 * 8, dtype=np.complex64)
    out = imp.drop_blocks(x, block=2048, drop_every=4)
    assert np.all(out[:2048] == 0) and np.all(out[2048:2048 * 4] == 1)
    assert np.all(x == 1)


def test_quantise_8bit_has_at_most_256_levels_per_rail():
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(5000) + 1j * rng.standard_normal(5000)).astype(np.complex64)
    out = imp.quantise_8bit(x)
    assert len(np.unique(out.real)) <= 256 and len(np.unique(out.imag)) <= 256
    assert len(np.unique(out.real)) > 50


def test_awgn_hits_requested_snr_and_is_deterministic():
    x = _tone(n=48_000)
    out = imp.awgn(x, 10.0, seed=3)
    snr = 10 * np.log10(np.mean(np.abs(x) ** 2) / np.mean(np.abs(out - x) ** 2))
    assert snr == pytest.approx(10.0, abs=0.3)
    np.testing.assert_array_equal(out, imp.awgn(x, 10.0, seed=3))
    assert not np.array_equal(out, imp.awgn(x, 10.0, seed=4))


def test_awgn_real_audio_stays_real():
    out = imp.awgn(np.sin(np.arange(1000) / 10).astype(np.float32), 20.0)
    assert not np.iscomplexobj(out)


def test_inject_nonfinite_places_bad_samples_only():
    x = _tone()
    out = imp.inject_nonfinite(x, 0.01, seed=0)
    bad = ~np.isfinite(out)
    assert 0 < bad.sum() <= 0.01 * len(x) + 1
    np.testing.assert_array_equal(out[~bad], x[~bad])
    assert np.all(np.isfinite(x))


def test_registry_profiles_apply_and_clean_is_identity():
    required = {"clean", "ppm_20", "dc_spike", "iq_swap", "drops", "8bit", "low_snr"}
    assert required <= set(imp.PROFILES)
    x = _tone()
    np.testing.assert_array_equal(imp.apply_profile("clean", x, RATE), x)
    for name in imp.PROFILES:
        out = imp.apply_profile(name, x, RATE, seed=1)
        assert out.shape == x.shape
