"""Synthesised ALE 2G round trips (own 8-FSK modulator -> AleDecoder)."""

import numpy as np

from msdr.decoder.pydecoders.ale import (
    AleDecoder,
    encode_ale_message,
    golay_decode,
    golay_encode,
    tone_hz,
)

_FS = 8000
_SYM = 64  # 125 baud


def _modulate(tones, *, lead=1234, tail=_FS * 2, amp=0.5, noise=0.0, seed=0) -> np.ndarray:
    """Phase-continuous 8-FSK, preceded by `lead` samples of silence (an
    arbitrary, non-symbol-aligned start) and followed by `tail` of silence."""
    freq = np.repeat([tone_hz(t) for t in tones], _SYM)
    sig = amp * np.sin(2 * np.pi * np.cumsum(freq) / _FS)
    audio = np.concatenate((np.zeros(lead), sig, np.zeros(tail)))
    rng = np.random.default_rng(seed)
    return (audio + rng.standard_normal(len(audio)) * noise).astype(np.float32)


def _run(decoder, audio, chunk=800):
    events = []
    for i in range(0, len(audio), chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    return events


def test_golay_corrects_three_errors_and_flags_four():
    for data in (0, 0xABC, 0xFFF):
        word = golay_encode(data)
        assert golay_decode(word) == (data, 0)
        assert golay_decode(word ^ 0b100000000000000000100001) == (data, 3)
        assert golay_decode(word ^ 0b1010101) is None  # 4 errors: detected


def test_ale_sounding_round_trip_with_noise():
    tones = encode_ale_message([("TWAS", "ABC123")])  # two words
    events = _run(AleDecoder(), _modulate(tones, noise=0.25))
    assert [e.text for e in events] == ["TWAS ABC123"]
    assert events[0].fields["call_type"] == "sound"
    assert events[0].fields["from"] == "ABC123"
    assert events[0].fields["words"] == 2


def test_ale_call_round_trip_reports_both_addresses():
    tones = encode_ale_message([("TO", "X"), ("TIS", "Y")])
    (event,) = _run(AleDecoder(), _modulate(tones, noise=0.2, lead=777, seed=1))
    assert event.text == "TO X TIS Y"
    assert event.fields["call_type"] == "call"
    assert (event.fields["to"], event.fields["from"]) == ("X", "Y")
    assert event.decoder == "ale" and event.kind == "text"


def test_ale_survives_a_burst_of_wrong_tones_via_three_copy_vote():
    tones = encode_ale_message([("TO", "BOB"), ("TIS", "AL")])
    tones[10] = (tones[10] + 3) % 8  # one bad tone per word at most hits one copy of a bit
    tones[60] = (tones[60] + 5) % 8
    (event,) = _run(AleDecoder(), _modulate(tones))
    assert event.text == "TO BOB TIS AL"


def test_ale_flush_emits_call_still_in_progress():
    tones = encode_ale_message([("TWAS", "N0CALL")])
    dec = AleDecoder()
    assert _run(dec, _modulate(tones, tail=_SYM * 20)) == []
    assert [e.text for e in dec.flush()] == ["TWAS N0CALL"]


def test_ale_no_event_on_noise():
    rng = np.random.default_rng(4)
    noise = (rng.standard_normal(_FS * 10) * 0.3).astype(np.float32)
    dec = AleDecoder()
    assert _run(dec, noise) == [] and dec.flush() == []
