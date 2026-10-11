import time

import numpy as np
import pytest

from msdr.decoder.channelizer import NFMChannel, PolyphaseChannelizer


def _tone(freq_hz: float, fs: int, n_samples: int) -> np.ndarray:
    t = np.arange(n_samples) / fs
    return np.exp(1j * 2 * np.pi * freq_hz * t).astype(np.complex64)


def test_tone_lands_in_expected_channel_with_rejection():
    fs = 8_000_000
    channelizer = PolyphaseChannelizer(fs, spacing_hz=12500)
    assert channelizer.M == 640
    assert channelizer.spacing_hz == 12500.0

    k_target = 11
    freq_hz = k_target * channelizer.spacing_hz
    assert freq_hz == 137_500

    n_samples = int(fs * 0.05)
    x = _tone(freq_hz, fs, n_samples)
    out = channelizer.process(x)

    # Skip the filter's startup transient; use the steady-state second half.
    steady = out[out.shape[0] // 2 :]
    mean_mag = np.abs(steady).mean(axis=0)

    assert np.argmax(mean_mag) == k_target
    # Unity gain for a bin-centred tone (spec: "keeps its amplitude").
    assert mean_mag[k_target] == pytest.approx(1.0, rel=0.05)

    for neighbor in (k_target - 2, k_target - 1, k_target + 1, k_target + 2):
        rejection_db = 20 * np.log10(mean_mag[k_target] / max(mean_mag[neighbor], 1e-12))
        assert rejection_db > 40, f"channel {neighbor} rejection only {rejection_db:.1f} dB"


def test_channel_for_matches_process():
    fs = 8_000_000
    channelizer = PolyphaseChannelizer(fs, spacing_hz=12500)
    center_hz = 152_800_000

    k, residual = channelizer.channel_for(center_hz + 137_500, center_hz)
    assert k == 11
    assert abs(residual) < 1e-6

    # A frequency 3 kHz off a bin centre must resolve to that bin with a
    # residual of exactly 3 kHz (spec: |residual| <= spacing_hz/2).
    k2, residual2 = channelizer.channel_for(center_hz + 137_500 + 3000, center_hz)
    assert k2 == 11
    assert abs(residual2 - 3000) < 1e-6
    assert abs(residual2) <= channelizer.spacing_hz / 2

    assert channelizer.channel_center_hz(k, center_hz) == center_hz + 137_500


def test_negative_offset_wraps_to_high_k():
    fs = 8_000_000
    channelizer = PolyphaseChannelizer(fs, spacing_hz=12500)
    center_hz = 152_800_000
    k, residual = channelizer.channel_for(center_hz - 137_500, center_hz)
    assert k == channelizer.M - 11
    assert abs(residual) < 1e-6
    assert channelizer.channel_center_hz(k, center_hz) == center_hz - 137_500


def test_50ms_block_at_8ms_processes_well_under_200ms():
    fs = 8_000_000
    channelizer = PolyphaseChannelizer(fs, spacing_hz=12500)
    n_samples = int(fs * 0.05)
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples)).astype(np.complex64)

    channelizer.process(x)  # warm up (fills the delay line)
    t0 = time.perf_counter()
    channelizer.process(x)
    elapsed_s = time.perf_counter() - t0
    print(f"\nchannelizer.process(50ms @ 8 MS/s) took {elapsed_s * 1000:.1f} ms")
    assert elapsed_s < 0.2


def test_nfm_channel_recovers_known_deviation():
    out_rate = 25000.0
    dev_hz = 3000.0  # within the +-5000 Hz NBFM max deviation NFMChannel assumes
    n_samples = int(out_rate * 0.5)
    t = np.arange(n_samples) / out_rate
    ch = np.exp(1j * 2 * np.pi * dev_hz * t).astype(np.complex64)

    nfm = NFMChannel(out_rate, residual_hz=0.0)
    pcm = nfm.process(ch)
    assert pcm

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    steady = samples[len(samples) // 2 :]  # past the AGC/resampler settling time
    assert steady.mean() > 0.1  # positive deviation -> positive recovered audio
    assert steady.std() < 0.05  # a constant deviation should decode to a near-DC value

    nfm_neg = NFMChannel(out_rate, residual_hz=0.0)
    pcm_neg = nfm_neg.process(np.exp(-1j * 2 * np.pi * dev_hz * t).astype(np.complex64))
    samples_neg = np.frombuffer(pcm_neg, dtype="<i2").astype(np.float32) / 32768.0
    assert samples_neg[len(samples_neg) // 2 :].mean() < -0.1


def test_nfm_channel_at_48khz_preserves_deviation_tone():
    # dsd-fme decoders (S6) need NFMChannel resampled to 48,000 Hz rather
    # than the P1 default of 22,050 Hz -- target_rate is per-spec now.
    out_rate = 96000.0
    dev_hz = 3000.0
    target_rate = 48000
    n_samples = int(out_rate * 0.2)
    t = np.arange(n_samples) / out_rate
    ch = np.exp(1j * 2 * np.pi * dev_hz * t).astype(np.complex64)

    nfm = NFMChannel(out_rate, residual_hz=0.0, target_rate=target_rate)
    pcm = nfm.process(ch)
    assert pcm

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    expected_len = n_samples * target_rate / out_rate
    assert abs(len(samples) - expected_len) < expected_len * 0.05

    steady = samples[len(samples) // 2 :]
    assert steady.mean() > 0.1  # positive deviation recovered at the new rate
    assert steady.std() < 0.05


def test_nfm_channel_fine_mixes_residual():
    # A tone offset by exactly `residual_hz` from the channel centre should
    # discriminate the same as a centred tone once the fine mixer removes it.
    out_rate = 25000.0
    residual_hz = 2000.0
    n_samples = int(out_rate * 0.5)
    t = np.arange(n_samples) / out_rate
    ch = np.exp(1j * 2 * np.pi * residual_hz * t).astype(np.complex64)  # pure carrier, offset only

    nfm = NFMChannel(out_rate, residual_hz=residual_hz)
    pcm = nfm.process(ch)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    steady = samples[len(samples) // 2 :]
    # Fine-mixed out, the discriminator should see ~0 Hz deviation -> quiet.
    assert abs(steady.mean()) < 0.05
