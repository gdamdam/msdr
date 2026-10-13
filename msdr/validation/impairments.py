"""Synthetic radio/ADC impairments for the scorecard and fuzz tests.

A decoder fed only by the app's own generator proves the receiver agrees
with the generator, not that it survives a real front end. These pure numpy
functions degrade a clean synthetic signal the way real hardware does
(oscillator error, DC spike, I/Q imbalance, 8-bit ADC, dropped USB blocks,
noise). Every function is deterministic for a given `seed` and returns a new
array; inputs are never modified. They are SYNTHETIC -- passing them is not
off-air validation.

Complex functions take complex IQ; `awgn`, `quantise_8bit`, `drop_blocks`
and `inject_nonfinite` also accept real audio.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from msdr.validation import fixtures as fx


def frequency_offset(iq: np.ndarray, offset_hz: float, rate: float) -> np.ndarray:
    """Carrier offset: multiply by exp(j*2*pi*f*n/rate)."""
    n = np.arange(len(iq), dtype=np.float64)
    return (iq * np.exp(2j * np.pi * offset_hz * n / rate)).astype(np.complex64)


def sample_clock_error(x: np.ndarray, ppm: float) -> np.ndarray:
    """Sample-clock error: the ADC runs `ppm` fast, so the signal is read
    off a slightly stretched/compressed time axis (linear interpolation).
    Output length equals input length."""
    n = len(x)
    t = np.arange(n, dtype=np.float64) * (1.0 + ppm * 1e-6)
    if np.iscomplexobj(x):
        real = np.interp(t, np.arange(n), x.real)
        imag = np.interp(t, np.arange(n), x.imag)
        return (real + 1j * imag).astype(np.complex64)
    return np.interp(t, np.arange(n), x).astype(np.float32)


def ppm_error(iq: np.ndarray, ppm: float, rate: float, carrier_hz: float) -> np.ndarray:
    """A crystal that is `ppm` off shifts both the carrier (ppm * carrier_hz)
    and the sample clock; apply both so they stay consistent."""
    shifted = frequency_offset(iq, ppm * 1e-6 * carrier_hz, rate)
    return sample_clock_error(shifted, ppm)


def off_centre(iq: np.ndarray, rate: float, fraction: float = 0.1) -> np.ndarray:
    """Signal tuned `fraction` of the sample rate away from the centre."""
    return frequency_offset(iq, fraction * rate, rate)


def dc_spike(iq: np.ndarray, level: float = 0.2) -> np.ndarray:
    """Constant DC offset on both rails (the LO leakage spike at 0 Hz)."""
    return (iq + level * (1 + 1j)).astype(np.complex64)


def iq_swap(iq: np.ndarray) -> np.ndarray:
    """Swap I and Q (wrong channel order): Q + jI == j*conj(x), a spectral
    mirror."""
    return (iq.imag + 1j * iq.real).astype(np.complex64)


def iq_imbalance(iq: np.ndarray, gain_db: float = 1.0, phase_deg: float = 5.0) -> np.ndarray:
    """Gain and phase mismatch between the I and Q mixers."""
    g = 10 ** (gain_db / 20)
    phi = np.deg2rad(phase_deg)
    i = iq.real
    q = g * (iq.imag * np.cos(phi) + iq.real * np.sin(phi))
    return (i + 1j * q).astype(np.complex64)


def drop_blocks(x: np.ndarray, block: int = 2048, drop_every: int = 4) -> np.ndarray:
    """Zero one block in every `drop_every` (lost USB transfers). Delegates
    to the scorecard fixture so there is a single definition."""
    return fx.dropped_blocks_iq(x, block=block, drop_every=drop_every)


def lost_frame(x: np.ndarray, block: int = 1024, at: float = 0.5) -> np.ndarray:
    """One IQ frame lost on queue overflow, as decoders see it after the IQ
    session zero-fills the gap (msdr.decoder.iq_session.gap_fill): a single
    zeroed block with timing intact."""
    out = x.copy()
    start = int(len(out) * at)
    out[start : start + block] = 0
    return out


def quantise_8bit(x: np.ndarray, full_scale: float | None = None) -> np.ndarray:
    """8-bit ADC: round each rail to 256 levels. `full_scale` is the input
    amplitude mapped to +/-127 (default: the signal's own peak, i.e. a
    perfectly set AGC); larger samples clip."""
    if full_scale is None:
        full_scale = float(np.max(np.abs(np.concatenate([x.real.ravel(), x.imag.ravel()]))))
    if full_scale <= 0:
        return x.copy()
    step = full_scale / 127.0

    def _q(r: np.ndarray) -> np.ndarray:
        return np.clip(np.rint(r / step), -128, 127) * step

    if np.iscomplexobj(x):
        return (_q(x.real) + 1j * _q(x.imag)).astype(np.complex64)
    return _q(x).astype(np.float32)


def awgn(x: np.ndarray, snr_db: float, seed: int = 0) -> np.ndarray:
    """Add white Gaussian noise at `snr_db` relative to the *active* signal
    power (samples above 1% of peak power), so a short burst padded with
    silence is not under-noised."""
    rng = np.random.default_rng(seed)
    power = np.abs(x) ** 2
    peak = float(power.max()) if power.size else 0.0
    if peak <= 0:
        return x.copy()
    sig_power = float(power[power > 0.01 * peak].mean())
    sigma2 = sig_power / 10 ** (snr_db / 10)
    if np.iscomplexobj(x):
        noise = (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x))) * np.sqrt(
            sigma2 / 2
        )
        return (x + noise).astype(np.complex64)
    return (x + rng.standard_normal(len(x)) * np.sqrt(sigma2)).astype(np.float32)


def inject_nonfinite(x: np.ndarray, fraction: float = 1e-4, seed: int = 0) -> np.ndarray:
    """Overwrite a random `fraction` of samples with NaN / +inf / -inf
    (a buggy upstream stage; floats only -- int16 IQ cannot carry these)."""
    rng = np.random.default_rng(seed)
    out = x.copy()
    count = max(1, int(len(x) * fraction))
    idx = rng.choice(len(x), size=count, replace=False)
    bad = rng.choice([np.nan, np.inf, -np.inf], size=count)
    out[idx] = bad.astype(out.dtype) if not np.iscomplexobj(out) else bad + 0j
    return out


# --- named profiles ------------------------------------------------------


@dataclass(frozen=True)
class ImpairmentProfile:
    name: str
    description: str
    apply: Callable[[np.ndarray, float, int, float], np.ndarray]  # (iq, rate, seed, carrier_hz)
    finite_only: bool = False  # True: output carries NaN/inf, not representable as int16 IQ


def _p(name, description, fn, finite_only=False) -> ImpairmentProfile:
    return ImpairmentProfile(name, description, fn, finite_only)


PROFILES: dict[str, ImpairmentProfile] = {
    p.name: p
    for p in (
        _p("clean", "no impairment", lambda iq, rate, seed, fc: iq),
        _p(
            "ppm_20",
            "+20 ppm crystal: carrier offset and sample clock",
            lambda iq, rate, seed, fc: ppm_error(iq, 20.0, rate, fc),
        ),
        _p(
            "off_centre",
            "signal tuned 10% of the sample rate off centre",
            lambda iq, rate, seed, fc: off_centre(iq, rate, 0.1),
        ),
        _p("dc_spike", "0.2 DC offset on both rails", lambda iq, rate, seed, fc: dc_spike(iq)),
        _p("iq_swap", "I and Q swapped", lambda iq, rate, seed, fc: iq_swap(iq)),
        _p(
            "iq_imbalance",
            "1 dB gain / 5 degree phase I/Q imbalance",
            lambda iq, rate, seed, fc: iq_imbalance(iq),
        ),
        _p(
            "drops",
            "one 2048-sample block in four dropped",
            lambda iq, rate, seed, fc: drop_blocks(iq),
        ),
        _p(
            "lost_frame",
            "one 1024-sample IQ frame lost mid-signal, gap zero-filled",
            lambda iq, rate, seed, fc: lost_frame(iq),
        ),
        _p("8bit", "8-bit ADC quantisation", lambda iq, rate, seed, fc: quantise_8bit(iq)),
        _p(
            "low_snr",
            "AWGN at 12 dB SNR",
            lambda iq, rate, seed, fc: awgn(iq, 12.0, seed),
        ),
        _p(
            "nonfinite",
            "0.01% of samples are NaN/inf",
            lambda iq, rate, seed, fc: inject_nonfinite(iq, 1e-4, seed),
            finite_only=True,
        ),
    )
}


def apply_profile(
    name: str, iq: np.ndarray, rate: float, seed: int = 0, carrier_hz: float = 162e6
) -> np.ndarray:
    return PROFILES[name].apply(iq, rate, seed, carrier_hz)
