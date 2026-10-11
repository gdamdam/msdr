import numpy as np

from msdr.decoder.peaks import PeakDetector


def _iq_block(
    tones_hz: list[float], fs: int, n: int, noise_std: float, seed: int = 0
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n) / fs
    x = rng.normal(0, noise_std, n) + 1j * rng.normal(0, noise_std, n)
    for freq_hz in tones_hz:
        x = x + np.exp(1j * 2 * np.pi * freq_hz * t)
    return x.astype(np.complex64)


def test_two_tones_found_with_correct_frequency_and_dc_excluded():
    fs = 2_000_000
    center_hz = 100_000_000
    nfft = 8192
    tone_a = 50_000.0
    tone_b = -300_000.0

    detector = PeakDetector(fs, center_hz, nfft=nfft, threshold_db=10.0, merge_hz=10000, history=3)

    # Two independent noise draws, like consecutive real IQ frames -- a
    # stray noise bin that happens to cross threshold in one draw won't
    # land near the same frequency in the next, so only the real tones
    # (present, at the same frequency, in both) get confirmed.
    detector.update(_iq_block([tone_a, tone_b, 0.0], fs, nfft, noise_std=0.05, seed=1))
    peaks = detector.update(_iq_block([tone_a, tone_b, 0.0], fs, nfft, noise_std=0.05, seed=2))

    found_freqs = sorted(p.frequency_hz for p in peaks)
    assert len(found_freqs) == 2  # the DC tone must never appear
    assert abs(found_freqs[0] - (center_hz + tone_b)) < 1000
    assert abs(found_freqs[1] - (center_hz + tone_a)) < 1000
    for p in peaks:
        assert abs(p.frequency_hz - center_hz) > 3000  # DC-exclusion band never returned


def test_oneoff_peak_not_returned_until_seen_twice():
    fs = 2_000_000
    center_hz = 100_000_000
    nfft = 8192
    tone_hz = 200_000.0

    detector = PeakDetector(fs, center_hz, nfft=nfft, threshold_db=10.0, merge_hz=10000, history=3)
    block = _iq_block([tone_hz], fs, nfft, noise_std=0.05)

    peaks_first = detector.update(block)
    assert peaks_first == []  # one-off: not confirmed yet

    peaks_second = detector.update(block)
    assert any(abs(p.frequency_hz - (center_hz + tone_hz)) < 1000 for p in peaks_second)


def test_retune_clears_history():
    fs = 2_000_000
    center_hz = 100_000_000
    nfft = 8192
    tone_hz = 200_000.0

    detector = PeakDetector(fs, center_hz, nfft=nfft, threshold_db=10.0, merge_hz=10000, history=3)
    block = _iq_block([tone_hz], fs, nfft, noise_std=0.05)
    detector.update(block)
    detector.update(block)
    assert detector.update(block) != []

    detector.retune(fs, center_hz + 5_000_000)
    # Same block, new span: history is reset, so the first update after a
    # retune must not immediately confirm a peak.
    assert detector.update(block) == []
