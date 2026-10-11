"""Conservative Morse evidence from an uninterrupted narrow-channel IQ dwell."""

import numpy as np

from msdr.survey.features import keying_timing


def cw_evidence(iq: np.ndarray, rate: float) -> dict | None:
    """Require a dominant narrow carrier, deep key-up gaps and dot/dash timing.

    Measure before active runs are normalized or concatenated. No protocol or
    station identity is inferred from frequency, and a steady tone is not CW.
    """
    if rate <= 0 or len(iq) < rate * 0.5 or not np.all(np.isfinite(iq)):
        return None
    nfft = min(4096, 2 ** int(np.floor(np.log2(len(iq)))))
    blocks = iq[: len(iq) // nfft * nfft].reshape(-1, nfft)
    psd = np.mean(abs(np.fft.fft(blocks * np.hanning(nfft), axis=1)) ** 2, axis=0)
    freq = np.fft.fftfreq(nfft, 1 / rate)
    band = abs(freq) < rate * 0.35
    floor = float(np.median(psd[band]))
    excess = np.maximum(psd - 2 * floor, 0) * band
    total = float(excess.sum())
    if total <= 0:
        return None
    peak = int(np.argmax(excess))
    offset = float(freq[peak])
    near = abs(freq - offset) <= 150
    concentration = float(excess[near].sum() / total)
    if concentration < 0.8 or psd[peak] < 20 * max(floor, 1e-20):
        return None
    # Isolate the detected line before envelope timing; leave the original
    # wide-channel measurements untouched for other modulation families.
    bins = np.fft.fftfreq(len(iq), 1 / rate)
    filtered = np.fft.ifft(np.fft.fft(iq) * (abs(bins - offset) <= 200))
    size = max(1, round(rate * 0.005))
    power = np.mean(abs(filtered[: len(iq) // size * size].reshape(-1, size)) ** 2, axis=1)
    low, high = np.percentile(power, [10, 90])
    if high <= 0 or high < 10 * max(low, 1e-20):
        return None
    on = power > (low + 0.25 * (high - low))
    edges = np.diff(np.r_[False, on, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    runs = [
        (end - start) * size / rate
        for start, end in zip(starts, ends, strict=True)
        if start > 1 and end < len(on) - 1
    ]
    timing = keying_timing(runs)
    if timing["fits_cw"] is not True or not 0.02 <= timing["unit_s"] <= 0.3:
        return None
    return {
        **timing,
        "offset_hz": offset,
        "carrier_concentration": concentration,
        "method": "isolated_carrier_morse_timing",
        "status": "tentative",
    }
