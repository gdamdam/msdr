import numpy as np
import pytest

# The probe resamples with SciPy, an RF-ML (`msdr[ml]`) dependency.
pytest.importorskip("scipy")

from scripts.rfml_input_probe import probe, variants  # noqa: E402


def test_variants_preserve_offset_sign_and_resampled_duration():
    rate = 50000
    iq = np.exp(2j * np.pi * 2000 * np.arange(50000) / rate).astype(np.complex64)
    cases = {name: (samples, sr) for name, samples, sr in variants(iq, rate)}
    shifted, sr = cases['offset_+1000_hz']
    measured = np.angle(shifted[1:] * shifted[:-1].conj()).mean() * sr / (2 * np.pi)
    assert measured == pytest.approx(3000, abs=1)
    for name in ['resample_1_2', 'resample_2_1']:
        samples, sr = cases[name]
        assert samples.size / sr == 1


def test_probe_rejects_truncated_or_noncomplex_reference(tmp_path):
    path = tmp_path / 'short.npy'
    np.save(path, np.ones(4096, dtype=np.complex64))
    with pytest.raises(ValueError, match='full second'):
        probe(path, 50000, [0], None)
    np.save(path, np.ones(50000))
    with pytest.raises(ValueError, match='complex'):
        probe(path, 50000, [0], None)
