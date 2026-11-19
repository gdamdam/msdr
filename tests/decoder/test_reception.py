"""Reception check: each finding appears for the condition it names, and a
clean, centred, strong signal gets none."""

from __future__ import annotations

import numpy as np

from msdr.decoder.reception import SpanStats, reception_check

_RATE = 2_000_000
_BLOCK = 65_536
_NOISE = 0.002


def _span(signals=(), *, blocks=6, noise=_NOISE, seed=0, gain=1.0, dc=0.0) -> SpanStats:
    """`signals`: (offset_hz, amplitude) narrow FM carriers on a noise floor."""
    rng = np.random.default_rng(seed)
    stats = SpanStats()
    t = np.arange(_BLOCK) / _RATE
    for _ in range(blocks):
        iq = (rng.standard_normal(_BLOCK) + 1j * rng.standard_normal(_BLOCK)) * noise
        for offset_hz, amplitude in signals:
            # A few kHz of slow FM so the power is spread over the channel
            # like a real narrowband signal, not a single bin.
            wobble = 3_000.0 * np.sin(2 * np.pi * 700.0 * t + rng.uniform(0, 6.28))
            phase = 2 * np.pi * np.cumsum(offset_hz + wobble) / _RATE
            iq = iq + amplitude * np.exp(1j * phase)
        iq = np.clip((iq + dc).real * gain, -1, 1) + 1j * np.clip((iq + dc).imag * gain, -1, 1)
        stats.update(iq.astype(np.complex64))
    return stats


def _codes(stats, offset_hz=200_000.0, **kwargs) -> list[str]:
    found = reception_check(stats, sample_rate_hz=_RATE, offset_hz=offset_hz, **kwargs)
    assert all(f["text"] and f["level"] in {"problem", "note"} for f in found)
    return [f["code"] for f in found]


def test_a_clean_centred_signal_has_no_findings():
    assert _codes(_span([(200_000.0, 0.05)])) == []


def test_no_iq_says_nothing():
    assert _codes(SpanStats()) == []


def test_empty_channel_is_no_signal():
    assert _codes(_span()) == ["no_signal"]


def test_weak_signal_is_a_note_not_a_problem():
    found = reception_check(
        _span([(200_000.0, 0.0012)]), sample_rate_hz=_RATE, offset_hz=200_000.0
    )
    assert [(f["code"], f["level"]) for f in found] == [("weak_signal", "note")]


def test_overload_is_reported_and_silences_spectrum_findings():
    # Clipped samples spray distortion over the span: only the overload (the
    # cause) is reported, not what the ruined spectrum then looks like.
    assert _codes(_span([(200_000.0, 0.05)], gain=40.0)) == ["overload"]


def test_signal_off_the_tested_frequency_gives_the_measured_shift():
    found = reception_check(
        _span([(206_000.0, 0.05)]), sample_rate_hz=_RATE, offset_hz=200_000.0
    )
    shift = next(f for f in found if f["code"] == "off_frequency")
    assert abs(shift["suggested_offset_hz"] - 6_000.0) < 1_500.0
    assert "above" in shift["text"]


def test_strong_neighbour_is_noted_beside_a_weaker_signal():
    codes = _codes(_span([(200_000.0, 0.01), (225_000.0, 0.4)]))
    assert "strong_neighbour" in codes
    assert "no_signal" not in codes


def test_dc_spike_on_the_channel():
    assert "dc_spike" in _codes(_span([(1_000.0, 0.02)], dc=0.05), offset_hz=1_000.0)
    # The same spike is no finding for a channel elsewhere in the span.
    assert "dc_spike" not in _codes(_span([(200_000.0, 0.05)], dc=0.05))


def test_channel_at_the_span_edge():
    assert _codes(_span(), offset_hz=950_000.0) == ["span_edge"]


def test_dropped_samples_are_reported_first_with_other_findings():
    assert _codes(_span(), dropped_gaps=3) == ["dropped_samples", "no_signal"]
