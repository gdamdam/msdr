"""Synthetic VOR/ILS Morse-ident tests: a ~1020 Hz AM tone keyed at 7 wpm
(standard timing, dot = 1.2/wpm s) atop the usual navigation subcarriers,
with AWGN and a receiver frequency offset."""

import asyncio
from types import SimpleNamespace

import numpy as np

from msdr.decoder.pydecoders.morse_ident import CHAR_TO_MORSE
from msdr.decoder.pydecoders.navigation import NavigationIQDecoder

_RATE = 48_000


def _ident_segments(text: str, wpm: float) -> list[tuple[bool, float]]:
    """One keyed occurrence of ``text``: (is_mark, duration_s) pairs. The
    trailing gap is zeroed -- the caller appends its own repeat/word gap."""
    dot = 1.2 / wpm
    segs: list[tuple[bool, float]] = []
    for ch in text:
        for sym in CHAR_TO_MORSE[ch]:
            segs.append((True, dot if sym == "." else 3 * dot))
            segs.append((False, dot))  # intra-letter symbol gap
        segs[-1] = (False, 3 * dot)  # inter-letter gap replaces the last symbol gap
    segs[-1] = (False, 0.0)
    return segs


def _segments_to_gate(segs: list[tuple[bool, float]], rate: int) -> np.ndarray:
    chunks = [np.full(max(1, round(dur * rate)), mark) for mark, dur in segs if dur > 0]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=bool)


def _ident_gate(
    text: str,
    rate: int,
    *,
    wpm: float = 7.0,
    repeats: int = 2,
    # The adaptive noise floor needs its 20 bins (each dot10/4 ~ 0.03 s, so
    # ~0.6 s total) to fill at least once after the decoder is created;
    # until then some bins are still at their reset-time zero, which can
    # read as a huge SNR. 1.2 s of lead-in clears that warm-up with margin.
    lead_s: float = 2.5,
    repeat_gap_s: float = 3.0,
    trail_s: float = 0.5,
) -> np.ndarray:
    segs: list[tuple[bool, float]] = [(False, lead_s)]
    one = _ident_segments(text, wpm)
    for _ in range(repeats):
        segs.extend(one)
        segs.append((False, repeat_gap_s))
    segs.append((False, trail_s))
    return _segments_to_gate(segs, rate)


def _ident_component(gate: np.ndarray, rate: int, depth: float) -> np.ndarray:
    t = np.arange(len(gate)) / rate
    return depth * gate * np.cos(2 * np.pi * 1020 * t)


def _vor_envelope(n: int, rate: int, phase_deg: float = 73.0) -> np.ndarray:
    t = np.arange(n) / rate
    subcarrier_phase = 2 * np.pi * 9960 * t + 16 * np.sin(
        2 * np.pi * 30 * t - np.deg2rad(phase_deg)
    )
    return 1 + 0.2 * np.cos(2 * np.pi * 30 * t) + 0.3 * np.cos(subcarrier_phase)


def _ils_envelope(n: int, rate: int) -> np.ndarray:
    t = np.arange(n) / rate
    return 1 + 0.12 * np.cos(2 * np.pi * 90 * t) + 0.04 * np.cos(2 * np.pi * 150 * t)


def _iq_bytes(
    envelope: np.ndarray,
    rate: int,
    *,
    offset_hz: float = 0.0,
    noise_std: float = 0.0,
    seed: int = 0,
) -> bytes:
    """Baseband envelope -> complex IQ at a receiver offset, plus AWGN on
    both channels, packed as signed interleaved int16 (as fed to NavigationIQDecoder)."""
    t = np.arange(len(envelope)) / rate
    iq = envelope.astype(np.complex128) * np.exp(2j * np.pi * offset_hz * t)
    if noise_std > 0:
        rng = np.random.default_rng(seed)
        iq = iq + rng.normal(0, noise_std, len(iq)) + 1j * rng.normal(0, noise_std, len(iq))
    scaled = np.column_stack((iq.real, iq.imag)) * 30_000
    return np.clip(np.rint(scaled), -32768, 32767).astype("<i2").tobytes()


def _run(
    decoder_id: str,
    text: str,
    *,
    noise_std: float,
    repeats: int = 2,
    offset_hz: float = 3_000.0,
    depth: float = 0.25,
):
    gate = _ident_gate(text, _RATE, repeats=repeats)
    base = (
        _vor_envelope(len(gate), _RATE) if decoder_id == "vor" else _ils_envelope(len(gate), _RATE)
    )
    envelope = base + _ident_component(gate, _RATE, depth)
    center = 115e6 if decoder_id == "vor" else 110e6
    decoder = NavigationIQDecoder(
        SimpleNamespace(id=decoder_id),
        sample_rate_hz=_RATE,
        center_hz=center,
        frequency_hz=center + offset_hz,
    )
    asyncio.run(decoder.start())
    decoder.feed(_iq_bytes(envelope, _RATE, offset_hz=offset_hz, noise_std=noise_std))
    events = []
    while not decoder._events.empty():
        event = decoder._events.get_nowait()
        if event is not None:
            events.append(event)
    return events


def test_vor_ident_decodes_at_reasonable_snr():
    events = _run("vor", "SFO", noise_std=0.01)
    idents = [e.fields["ident"] for e in events if "ident" in e.fields]
    assert idents, "expected a confirmed ident at this SNR"
    assert all(i == "SFO" for i in idents)
    last = [e for e in events if "ident" in e.fields][-1]
    assert last.fields["ident_repeats"] >= 2
    assert 0.0 < last.fields["ident_confidence"] <= 1.0
    # Navigation measurement itself must still be unaffected by ident decoding.
    assert "radial_deg" in last.fields


def test_ils_ident_decodes_at_reasonable_snr():
    events = _run("ils", "ISFO", noise_std=0.01)
    idents = [e.fields["ident"] for e in events if "ident" in e.fields]
    assert idents, "expected a confirmed ident at this SNR"
    assert all(i == "ISFO" for i in idents)
    last = [e for e in events if "ident" in e.fields][-1]
    assert last.fields["ident_repeats"] >= 2
    assert "ddm" in last.fields


def test_no_ident_at_very_low_snr_not_a_wrong_one():
    # Heavy AWGN: the ident must not decode -- never a wrong guess.
    events = _run("vor", "SFO", noise_std=2.0)
    idents = [e.fields["ident"] for e in events if "ident" in e.fields]
    assert idents == []


def test_two_repeat_rule_withholds_single_hearing():
    # Only one keyed occurrence (plus the trailing silence needed to flush
    # it): a single clean decode must not become a confirmed identity.
    events = _run("vor", "SFO", noise_std=0.01, repeats=1)
    idents = [e.fields["ident"] for e in events if "ident" in e.fields]
    assert idents == []


def test_two_repeat_rule_confirms_on_second_hearing():
    events = _run("vor", "SFO", noise_std=0.01, repeats=2)
    confirmations = [e.fields["ident_repeats"] for e in events if "ident" in e.fields]
    assert confirmations, "expected confirmation by the second repeat"
    assert confirmations[0] >= 2


def test_single_letter_is_never_an_ident():
    # "S" (three dots) passes upstream's raw-length filter; it was observed
    # as a false repeated ident near -9 dB, so idents must be 2-4 characters.
    events = _run("vor", "S", noise_std=0.01, repeats=3)
    assert [e.fields["ident"] for e in events if "ident" in e.fields] == []


def test_two_letter_ident_is_accepted():
    events = _run("vor", "SF", noise_std=0.01, repeats=2)
    idents = {e.fields["ident"] for e in events if "ident" in e.fields}
    assert idents == {"SF"}
