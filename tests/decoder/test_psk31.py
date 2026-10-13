"""Synthesised BPSK31 round trips (own modulator -> Psk31Decoder)."""

import numpy as np

from msdr.decoder.pydecoders.psk31 import _VARICODE, Psk31Decoder, encode_varicode

_FS = 8000
_SPS = 256  # 31.25 baud at 8 kHz


def _modulate(bits, fc_hz, *, amp=0.5, noise=0.0, phase=0.7, seed=0) -> np.ndarray:
    """Differential BPSK with raised-cosine symbol transitions: bit 0 flips the
    carrier phase (the envelope goes through zero half-way between symbol
    centres), bit 1 keeps it."""
    signs = [1.0]
    for b in bits:
        signs.append(signs[-1] if b else -signs[-1])
    u = np.arange(_SPS) / _SPS
    ramp = 0.5 * (1 + np.cos(np.pi * u))  # 1 -> 0 over one symbol
    pairs = zip(signs, signs[1:], strict=False)
    base = np.concatenate([s0 * ramp + s1 * (1 - ramp) for s0, s1 in pairs])
    t = np.arange(len(base)) / _FS
    audio = amp * base * np.cos(2 * np.pi * fc_hz * t + phase)
    rng = np.random.default_rng(seed)
    return (audio + rng.standard_normal(len(audio)) * noise).astype(np.float32)


def _run(decoder, audio, chunk=800):
    events = []
    for i in range(0, len(audio), chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    return events


def test_varicode_table_is_a_valid_prefix_free_code():
    codes = list(_VARICODE.values())
    assert len(set(codes)) == len(codes)
    assert all(c[0] == c[-1] == "1" and "00" not in c for c in codes)


def test_psk31_round_trip_with_offset_carrier_and_noise():
    idle = [0] * 100  # real transmissions start with idle (continuous reversals)
    bits = idle + encode_varicode("CQ CQ de W1AW pse k\n") + [0] * 20
    # 1003.7 Hz: off any bin and off the nominal 1000 Hz, to exercise AFC.
    audio = _modulate(bits, 1003.7, noise=0.25)
    events = _run(Psk31Decoder(), audio)
    assert [e.text for e in events] == ["CQ CQ de W1AW pse k"]
    assert events[0].decoder == "psk31" and events[0].kind == "text"
    assert abs(events[0].fields["carrier_hz"] - 1003.7) < 3.0


def test_psk31_idle_pause_emits_partial_line():
    bits = [0] * 100 + encode_varicode("hello") + [0] * 90
    events = _run(Psk31Decoder(), _modulate(bits, 1500.0, noise=0.1))
    assert [e.text for e in events] == ["hello"]


def test_psk31_flush_emits_unterminated_line():
    dec = Psk31Decoder()
    # Too little idle after the text to trip the pause timeout.
    assert _run(dec, _modulate([0] * 100 + encode_varicode("73") + [0] * 10, 700.0)) == []
    assert [e.text for e in dec.flush()] == ["73"]


def test_psk31_no_event_on_noise():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(_FS * 8) * 0.3).astype(np.float32)
    dec = Psk31Decoder()
    assert _run(dec, noise) == [] and dec.flush() == []
