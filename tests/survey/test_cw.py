import numpy as np
import pytest

from msdr.survey.cw import cw_evidence


def waveform(runs, offset=2800, noise=0.1):
    rate = 50000
    envelope = [np.zeros(5000)]
    for duration in runs:
        envelope.extend([np.ones(round(rate * duration)), np.zeros(3000)])
    envelope.append(np.zeros(5000))
    a = np.concatenate(envelope)
    rng = np.random.default_rng(7)
    iq = a * np.exp(2j * np.pi * offset * np.arange(len(a)) / rate)
    iq += noise * (rng.normal(size=len(a)) + 1j * rng.normal(size=len(a)))
    return iq


@pytest.mark.parametrize("offset", [-4200, 0, 2800])
def test_noisy_offset_morse(offset):
    result = cw_evidence(waveform([0.06, 0.18] * 5, offset), 50000)
    assert result is not None
    assert abs(result["offset_hz"] - offset) < 15
    assert result["unit_s"] == pytest.approx(0.06, abs=0.015)


def test_not_every_burst_is_morse():
    assert cw_evidence(waveform([0.1] * 10), 50000) is None
    assert cw_evidence(waveform([1.5]), 50000) is None
    assert cw_evidence(waveform([0.06, 0.18] * 5, noise=5), 50000) is None


def test_wide_modulation_with_morse_like_bursts_is_not_cw():
    iq = waveform([0.06, 0.18] * 5)
    t = np.arange(len(iq)) / 50000
    fm = iq * np.exp(1j * 5 * np.sin(2 * np.pi * 1000 * t))
    assert cw_evidence(fm, 50000) is None


def test_invalid_iq():
    for iq in [np.zeros(100), np.zeros(50000), np.full(50000, np.nan)]:
        assert cw_evidence(iq, 50000) is None
