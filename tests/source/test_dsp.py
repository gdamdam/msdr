"""Streaming demodulators on synthetic IQ: pitch, SNR, squelch, RDS."""

from fractions import Fraction

import numpy as np
import pytest

from msdr.source.dsp import (
    ChannelExtractor,
    CwDemod,
    DemodChain,
    NfmDemod,
    SsbDemod,
    decimation_plan,
    iq16_to_complex,
)
from msdr.survey.rds import encode_rds_mpx, fm_modulate_chunks

_FS = 2_000_000
_AUDIO = 48_000


def _snr_db(x: np.ndarray, fs: float, f0: float) -> float:
    """Tone power (+-15 Hz) over everything else in the band (SINAD)."""
    x = x.astype(np.float64) - np.mean(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    f = np.fft.rfftfreq(x.size, 1 / fs)
    sig = np.abs(f - f0) <= 15
    return float(10 * np.log10(spec[sig].sum() / spec[~sig].sum()))


def _peak_hz(x: np.ndarray, fs: float) -> float:
    x = x.astype(np.float64) - np.mean(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.fft.rfftfreq(x.size, 1 / fs)[np.argmax(spec)])


def _noise(n: int, sigma: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (sigma * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex64)


def _run(chain, z: np.ndarray, frame: int = 49_999):
    """Feed complex IQ through int16 frames of an awkward size (state must
    carry across boundaries); returns (listening audio, flat tap or None)."""
    iq = (np.column_stack([z.real, z.imag]) * 32767).clip(-32768, 32767).astype("<i2")
    iq = iq.reshape(-1)
    audio, flat = [], []
    for s in range(0, z.size, frame):
        audio.append(chain.process(iq16_to_complex(iq[2 * s : 2 * (s + frame)].tobytes())))
        if chain.flat is not None:
            flat.append(chain.flat)
    return np.concatenate(audio), (np.concatenate(flat) if flat else None)


def _t(seconds: float, fs: float = _FS) -> np.ndarray:
    return np.arange(int(seconds * fs)) / fs


@pytest.mark.parametrize("fs_in", [250_000, 2_000_000, 8_000_000, 10_000_000])
@pytest.mark.parametrize("out_rate", [48_000, 250_000])
def test_decimation_plan_integer_stages(fs_in, out_rate):
    plan = decimation_plan(fs_in, out_rate)
    assert 0 <= len(plan.stages) <= 3
    assert all(2 <= d <= 10 for d in plan.stages)
    total = int(np.prod(plan.stages)) if plan.stages else 1
    assert Fraction(fs_in, total) * Fraction(plan.up, plan.down) == out_rate
    if out_rate == 250_000:
        assert (plan.up, plan.down) == (1, 1)  # WFM: pure integer decimation
    else:
        assert plan.mid_rate == 50_000  # voice: via 50 kHz, then 24/25


def test_extractor_is_block_size_independent():
    z = np.exp(2j * np.pi * 123_456 * _t(0.3)).astype(np.complex64) + _noise(600_000, 0.05)
    one = ChannelExtractor(_FS, 120_000, 48_000, 6_250).process(z)
    ex = ChannelExtractor(_FS, 120_000, 48_000, 6_250)
    parts = np.concatenate([ex.process(z[s : s + 7_777]) for s in range(0, z.size, 7_777)])
    assert parts.size == one.size
    np.testing.assert_allclose(parts, one, atol=1e-4)
    # The 3456 Hz residual tone lands where it should after the shift.
    assert abs(_peak_hz(np.real(one[2000:]), 48_000) - 3_456) < 5


def test_nfm_tone_both_taps():
    t = _t(1.0)
    z = 0.5 * np.exp(1j * (2 * np.pi * 250e3 * t + (2_500 / 1_000) * np.sin(2 * np.pi * 1e3 * t)))
    z = z.astype(np.complex64) + _noise(t.size, 0.01)
    chain = DemodChain(_FS, 250e3, "NFM", 12_500)
    audio, flat = _run(chain, z)
    assert audio.size == flat.size == 48_000
    assert _snr_db(flat[_AUDIO // 2 :], _AUDIO, 1_000) > 30
    assert _snr_db(audio[_AUDIO // 2 :], _AUDIO, 1_000) > 30
    # Flat tap: 2.5 kHz deviation == 0.5 on the 5 kHz scale, untouched by
    # de-emphasis/band-pass (which would scale a 1 kHz tone by ~0.2).
    assert np.std(flat[_AUDIO // 2 :]) * np.sqrt(2) == pytest.approx(0.5, rel=0.03)


def test_nfm_squelch_closes_on_noise_and_opens_on_signal():
    chain = DemodChain(_FS, 250e3, "NFM", 12_500)
    demod: NfmDemod = chain.demod
    demod.squelch_db = 10.0
    demod.reset()
    noise, flat = _run(chain, _noise(_FS // 2, 0.2, seed=3))
    assert demod.squelch_open is False
    assert np.all(noise[_AUDIO // 20 :] == 0)
    assert np.std(flat) > 0.1  # the flat tap never squelches
    t = _t(0.5)
    tone = 0.5 * np.exp(1j * (2 * np.pi * 250e3 * t + 2.5 * np.sin(2 * np.pi * 1e3 * t)))
    audio, _ = _run(chain, tone.astype(np.complex64) + _noise(t.size, 0.01, seed=4))
    assert demod.squelch_open is True
    assert _snr_db(audio[_AUDIO // 10 :], _AUDIO, 1_000) > 30
    # Carrier gone again -> closes (hysteresis doesn't hold it open on noise).
    audio, _ = _run(chain, _noise(_FS // 4, 0.2, seed=5))
    assert demod.squelch_open is False
    assert np.all(audio[_AUDIO // 20 :] == 0)


def test_am_30_percent_tone():
    t = _t(2.5)
    z = 0.3 * (1 + 0.3 * np.cos(2 * np.pi * 1e3 * t)) * np.exp(2j * np.pi * -310e3 * t)
    chain = DemodChain(_FS, -310e3, "AM", 10_000)
    audio, _ = _run(chain, z.astype(np.complex64) + _noise(t.size, 0.002))
    tail = audio[int(1.5 * _AUDIO) :]  # past the 0.3 s DC-block settling
    assert _peak_hz(tail, _AUDIO) == pytest.approx(1_000, abs=3)
    assert _snr_db(tail, _AUDIO, 1_000) > 30


@pytest.mark.parametrize(
    ("mode", "carrier_hz", "expect_hz"), [("USB", 1_000, 1_000), ("LSB", -1_000, 1_000)]
)
def test_ssb_pitch_is_exact(mode, carrier_hz, expect_hz):
    t = _t(1.0)
    z = 0.2 * np.exp(2j * np.pi * (400e3 + carrier_hz) * t)
    chain = DemodChain(_FS, 400e3, mode, 2_700)
    audio, _ = _run(chain, z.astype(np.complex64) + _noise(t.size, 0.001))
    tail = audio[_AUDIO // 4 :]
    assert _peak_hz(tail, _AUDIO) == pytest.approx(expect_hz, abs=2)
    assert _snr_db(tail, _AUDIO, expect_hz) > 30


def test_usb_rejects_lower_sideband():
    t = _t(0.5)
    levels = {}
    for carrier in (1_000, -1_000):
        chain = DemodChain(_FS, 400e3, "USB", 2_700)
        chain.demod = SsbDemod("USB", 2_700, agc=False)  # compare raw levels
        z = 0.2 * np.exp(2j * np.pi * (400e3 + carrier) * t)
        audio, _ = _run(chain, z.astype(np.complex64))
        levels[carrier] = np.std(audio[_AUDIO // 10 :])
    assert 20 * np.log10(levels[1_000] / levels[-1_000]) > 40


def test_cw_keyed_carrier_gives_700hz_bursts():
    fs = 250_000
    t = _t(1.2, fs)
    key = (np.floor(t / 0.1) % 2 == 0).astype(np.float64)  # 100 ms on / 100 ms off
    z = 0.2 * key * np.exp(2j * np.pi * 30e3 * t)
    chain = DemodChain(fs, 30e3, "CW", 500)
    assert isinstance(chain.demod, CwDemod)
    audio, _ = _run(chain, z.astype(np.complex64) + _noise(t.size, 1e-4), frame=12_345)
    on, off = [], []
    for k in range(2, 12):  # skip the first on/off pair (AGC settling)
        seg = audio[int((k * 0.1 + 0.02) * _AUDIO) : int(((k + 1) * 0.1 - 0.02) * _AUDIO)]
        (on if k % 2 == 0 else off).append(seg)
    on_rms = np.sqrt(np.mean(np.concatenate(on) ** 2))
    off_rms = np.sqrt(np.mean(np.concatenate(off) ** 2))
    assert 20 * np.log10(on_rms / off_rms) > 20
    for seg in on:
        assert _peak_hz(seg, _AUDIO) == pytest.approx(700, abs=15)


@pytest.mark.parametrize("pilot_level", [0.09, 0.0])
def test_wfm_mono_audio_and_rds(pilot_level):
    mpx = encode_rds_mpx("TESTFM", 0x54A8, None, 2.5, 250_000, pilot_level=pilot_level)
    chain = DemodChain(_FS, 400e3, "WFM", 200_000)
    audio = []
    for c in fm_modulate_chunks(mpx, 250_000, _FS, 400e3, 100_000, amplitude=0.3):
        audio.append(chain.process(c))
    audio = np.concatenate(audio)
    assert audio.size == pytest.approx(2.5 * _AUDIO, abs=200)
    assert _peak_hz(audio[_AUDIO:], _AUDIO) == pytest.approx(1_000, abs=3)
    assert _snr_db(audio[_AUDIO:], _AUDIO, 1_000) > 30
    res = chain.rds_parser.result()
    assert res["pi"] == "0x54A8"
    assert res["ps"] == "TESTFM"
