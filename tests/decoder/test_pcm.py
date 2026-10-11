import struct

import numpy as np
import pytest

from msdr.decoder.pcm import (
    Normalizer,
    RateEstimator,
    Resampler,
    to_mono_f32,
    to_s16,
    wav_stream_header,
)


def test_to_mono_f32_takes_first_channel_and_scales():
    # int16 stereo, L=R duplicated (SDRconnect's measured format).
    data = np.array([1000, 1000, -2000, -2000, 32767, 32767], dtype="<i2").tobytes()
    mono = to_mono_f32(data, channels=2)
    assert mono.shape == (3,)
    np.testing.assert_allclose(mono, np.array([1000, -2000, 32767]) / 32768.0, atol=1e-6)


def test_to_mono_f32_mono_passthrough():
    data = np.array([100, -100], dtype="<i2").tobytes()
    mono = to_mono_f32(data, channels=1)
    np.testing.assert_allclose(mono, np.array([100, -100]) / 32768.0, atol=1e-6)


def test_resampler_output_length_matches_ratio():
    in_rate, out_rate = 48000, 22050
    r = Resampler(in_rate, out_rate)
    duration_s = 2.0
    x = np.zeros(int(in_rate * duration_s), dtype=np.float32)
    out = r.process(x)
    expected = duration_s * out_rate
    # Streaming resampler trails a little (filter memory not yet flushed);
    # within a few samples of the expected count is a healthy result.
    assert abs(len(out) - expected) < 20


def test_resampler_preserves_tone_frequency():
    in_rate, out_rate = 48000, 22050
    tone_hz = 1000  # well under both Nyquist rates
    duration_s = 0.5
    t = np.arange(int(in_rate * duration_s)) / in_rate
    x = np.sin(2 * np.pi * tone_hz * t).astype(np.float32)

    r = Resampler(in_rate, out_rate)
    out = r.process(x)

    spectrum = np.abs(np.fft.rfft(out))
    freqs = np.fft.rfftfreq(len(out), d=1 / out_rate)
    peak_hz = freqs[np.argmax(spectrum)]
    assert abs(peak_hz - tone_hz) < 20  # generous tolerance for a short window


def test_resampler_carries_state_across_calls():
    """Feeding in two chunks must resample the same as one chunk (streaming state)."""
    in_rate, out_rate = 48000, 22050
    tone_hz = 800
    n = 4800
    t = np.arange(n) / in_rate
    x = np.sin(2 * np.pi * tone_hz * t).astype(np.float32)

    whole = Resampler(in_rate, out_rate).process(x)

    split = Resampler(in_rate, out_rate)
    half = n // 2
    out_a = split.process(x[:half])
    out_b = split.process(x[half:])
    chunked = np.concatenate([out_a, out_b])

    assert abs(len(whole) - len(chunked)) <= 1
    n_compare = min(len(whole), len(chunked))
    np.testing.assert_allclose(whole[:n_compare], chunked[:n_compare], atol=1e-3)


def test_normalizer_raises_quiet_signal_toward_target():
    # Short release so the steady state is reached within a small, fast sample count.
    normalizer = Normalizer(target_peak=0.5, release_s=0.01, rate=22050)
    quiet = np.full(5000, 0.01, dtype=np.float32)
    out = normalizer.process(quiet)
    # Instant attack: gain should have caught up to make the tail near target_peak.
    assert abs(out[-1]) == pytest.approx(0.5, abs=0.05)


def test_normalizer_silence_no_nan_or_blowup():
    normalizer = Normalizer()
    silence = np.zeros(1000, dtype=np.float32)
    out = normalizer.process(silence)
    assert np.all(np.isfinite(out))
    assert np.all(out == 0)


def test_to_s16_round_trip():
    x = np.array([-1.0, 0.0, 0.5, 1.0], dtype=np.float32)
    raw = to_s16(x)
    back = np.frombuffer(raw, dtype="<i2")
    np.testing.assert_array_equal(back, np.array([-32767, 0, 16383, 32767]))


def test_to_s16_clips_out_of_range():
    x = np.array([-2.0, 2.0], dtype=np.float32)
    raw = to_s16(x)
    back = np.frombuffer(raw, dtype="<i2")
    np.testing.assert_array_equal(back, np.array([-32767, 32767]))


def test_rate_estimator_waits_for_window_then_reports():
    est = RateEstimator(window_s=5.0)
    t0 = 1_700_000_000.0
    true_rate = 48000
    frame_s = 0.05  # matches the real audio stream's ~20 frames/s cadence
    samples_per_frame = int(true_rate * frame_s)
    result = None
    for i in range(1, 141):  # 7s of frames
        result = est.add(samples_per_frame, t0 + i * frame_s)
        if i * frame_s < 5.0:
            assert result is None
    assert result is not None
    assert abs(result - true_rate) / true_rate < 0.02


def test_rate_estimator_snap_picks_nearest_standard_rate():
    assert RateEstimator.snap(47990) == 48000
    assert RateEstimator.snap(22000) == 22050
    assert RateEstimator.snap(16200) == 16000


def test_wav_stream_header_is_44_bytes_with_ffffffff_sizes():
    header = wav_stream_header(22050, channels=1)
    assert len(header) == 44
    assert header[0:4] == b"RIFF"
    riff_size = struct.unpack("<I", header[4:8])[0]
    assert riff_size == 0xFFFFFFFF
    assert header[8:12] == b"WAVE"
    assert header[12:16] == b"fmt "
    fmt_size, audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack(
        "<IHHIIHH", header[16:36]
    )
    assert (fmt_size, audio_format, channels, rate, bits) == (16, 1, 1, 22050, 16)
    assert byte_rate == 22050 * block_align
    assert header[36:40] == b"data"
    data_size = struct.unpack("<I", header[40:44])[0]
    assert data_size == 0xFFFFFFFF
