from __future__ import annotations

import numpy as np
import pytest

from msdr.rfml.spectrogram import iq_spectrogram


def test_bin_centered_tone_is_centered_and_calibrated_in_dbfs():
    n_fft = 64
    sample_rate = 6400.0
    tone_bin = 7
    samples = np.arange(n_fft * 2)
    iq = np.exp(2j * np.pi * tone_bin * samples / n_fft)

    result = iq_spectrogram(
        iq,
        sample_rate_hz=sample_rate,
        center_frequency_hz=100_000.0,
        n_fft=n_fft,
        hop_length=n_fft,
    )

    # fftshift places DC at bin N/2; a positive tone advances from there.
    peak_bin = n_fft // 2 + tone_bin
    assert result.dbfs.shape == (2, n_fft)
    assert np.argmax(result.dbfs[0]) == peak_bin
    assert result.dbfs[0, peak_bin] == pytest.approx(0.0, abs=1e-5)
    assert result.dbfs.flags.writeable is False


def test_box_edges_map_to_bin_edges_and_union_of_frame_sample_bounds():
    n_fft = 8
    rate = 8.0
    result = iq_spectrogram(
        np.ones(20, dtype=np.complex64),
        sample_rate_hz=rate,
        center_frequency_hz=100.0,
        n_fft=n_fft,
        hop_length=4,
    )
    meta = result.metadata

    # Bin 4 is DC at 100 Hz; its edges are 99.5 and 100.5 Hz.
    dc = meta.box_to_coordinates((4, 0, 5, 1))
    assert (dc.frequency_start_hz, dc.frequency_end_hz) == (99.5, 100.5)
    assert (dc.sample_start, dc.sample_end) == (0, 8)
    assert (dc.time_start_s, dc.time_end_s) == (0.0, 1.0)

    multi = meta.box_to_coordinates((0, 1, 8, 3))
    assert (multi.sample_start, multi.sample_end) == (4, 16)
    assert (multi.time_start_s, multi.time_end_s) == (0.5, 2.0)
    assert multi.frequency_start_hz == pytest.approx(95.5)
    assert multi.frequency_end_hz == pytest.approx(103.5)


@pytest.mark.parametrize(
    "iq",
    [
        np.ones(16, dtype=np.float32),
        np.ones((2, 8), dtype=np.complex64),
        np.ones(7, dtype=np.complex64),
        np.array([1 + 0j, np.nan + 0j, *([1 + 0j] * 6)]),
    ],
)
def test_rejects_malformed_short_or_nonfinite_iq(iq):
    with pytest.raises(ValueError):
        iq_spectrogram(
            iq, sample_rate_hz=8.0, center_frequency_hz=0.0, n_fft=8
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate_hz": 0.0},
        {"sample_rate_hz": float("inf")},
        {"center_frequency_hz": float("nan")},
        {"n_fft": 7},
        {"n_fft": True},
        {"hop_length": 0},
        {"hop_length": 1.5},
        {"dynamic_range_db": 0.0},
        {"dynamic_range_db": float("nan")},
    ],
)
def test_rejects_invalid_configuration(kwargs):
    options = {
        "sample_rate_hz": 8.0,
        "center_frequency_hz": 0.0,
        "n_fft": 8,
    }
    options.update(kwargs)
    with pytest.raises(ValueError):
        iq_spectrogram(np.ones(8, dtype=np.complex64), **options)


@pytest.mark.parametrize(
    "box",
    [
        (0, 0, 0, 1),
        (0, 0, 9, 1),
        (0, 0, 1, 0),
        (0, 0, 1, 4),
        (0.0, 0, 1, 1),
        [0, 0, 1, 1],
        (False, 0, 1, 1),
    ],
)
def test_rejects_invalid_pixel_boxes(box):
    result = iq_spectrogram(
        np.ones(16, dtype=np.complex64),
        sample_rate_hz=8.0,
        center_frequency_hz=0.0,
        n_fft=8,
        hop_length=4,
    )
    with pytest.raises(ValueError):
        result.metadata.box_to_coordinates(box)
