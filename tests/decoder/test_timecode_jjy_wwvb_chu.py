"""Synthesised round trips for the JJY, WWVB and CHU time-code decoders.

Pulse widths and tone frequencies are re-declared here (not imported) next to
the production module's own bands, like test_timecode.py does."""

from datetime import UTC, datetime

import numpy as np

from msdr.decoder.pydecoders.timecode import (
    ChuDecoder,
    JjyDecoder,
    WwvbDecoder,
    chu_burst_bits,
    encode_chu_b_burst,
    encode_chu_burst,
    encode_jjy_minute,
    encode_wwvb_minute,
)

_FS = 11025
_CARRIER_HZ = 400.0
_LEAD_IN_S = 1.5  # keeps the decoder's unmeasurable first pulse out of the frame
_JJY_S = {"M": 0.2, "1": 0.5, "0": 0.8}
_WWVB_S = {"0": 0.2, "1": 0.5, "M": 0.8}


def _render(labels, nominal_s, *, high_start: bool, low_level: float) -> np.ndarray:
    """Per second, the first nominal_s[label] seconds are at one level and the
    rest at the other: high_start=True is JJY (full carrier then reduced),
    False is WWVB (reduced carrier then full)."""
    start_lvl, rest_lvl = (1.0, low_level) if high_start else (low_level, 1.0)
    chunks = [np.full(int(_LEAD_IN_S * _FS), low_level if high_start else 1.0)]
    for lbl in labels:
        n = int(round(nominal_s[lbl] * _FS))
        chunks += [np.full(n, start_lvl), np.full(_FS - n, rest_lvl)]
    env = np.concatenate(chunks)
    t = np.arange(len(env)) / _FS
    return (env * np.sin(2 * np.pi * _CARRIER_HZ * t)).astype(np.float32)


def _feed_all(decoder, audio: np.ndarray, chunk: int = 1102) -> list:
    events = []
    for i in range(0, len(audio), chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    return events


def _noise(seconds: float, sigma: float = 0.05) -> np.ndarray:
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(_FS * seconds)) * sigma).astype(np.float32)


# -- JJY ----------------------------------------------------------------------


def test_jjy_decodes_one_minute_frame_jst_to_utc():
    labels = encode_jjy_minute(datetime(2026, 9, 26, 5, 31))  # JST -> 2026-09-25 20:31Z
    audio = _render(labels, _JJY_S, high_start=True, low_level=0.1)
    (event,) = _feed_all(JjyDecoder(), audio)
    assert event.decoder == "jjy" and event.kind == "time"
    assert event.fields["utc"] == "2026-09-25T20:31:00+00:00"
    assert event.fields["station"] == "JJY"
    assert event.integrity == "passed"


def test_jjy_parity_error_is_flagged_but_still_decodes():
    labels = encode_jjy_minute(datetime(2026, 9, 26, 5, 31))
    labels[37] = "0" if labels[37] == "1" else "1"  # PA2 (minute parity)
    utc, extra, integrity = JjyDecoder()._decode_frame(labels)
    assert utc == datetime(2026, 9, 25, 20, 31, tzinfo=UTC)
    assert integrity["integrity"] == "failed" and extra["parity_ok"] is False


def test_jjy_no_event_on_noise():
    assert _feed_all(JjyDecoder(), _noise(10)) == []


# -- WWVB ---------------------------------------------------------------------


def test_wwvb_decodes_one_minute_frame_with_dut1():
    dt = datetime(2026, 9, 25, 20, 31, tzinfo=UTC)
    labels = encode_wwvb_minute(dt, dut1_s=-0.3)
    audio = _render(labels, _WWVB_S, high_start=False, low_level=0.14)
    (event,) = _feed_all(WwvbDecoder(), audio)
    assert event.decoder == "wwvb"
    assert event.fields["utc"] == dt.isoformat()
    assert event.fields["dut1"] == -0.3
    assert event.integrity == "passed"


def test_wwvb_rejects_out_of_range_fields():
    labels = encode_wwvb_minute(datetime(2026, 9, 25, 20, 31, tzinfo=UTC))
    labels[1:4] = ["1", "1", "1"]  # minute tens 70 -> impossible
    assert WwvbDecoder()._decode_frame(labels) is None


def test_wwvb_no_event_on_noise():
    assert _feed_all(WwvbDecoder(), _noise(10)) == []


# -- CHU ----------------------------------------------------------------------

_CLOCK_UTC = datetime(2026, 9, 25, 20, 31, 45, tzinfo=UTC)


def _fsk(bits: list[int], noise_sigma: float = 0.0) -> np.ndarray:
    """Continuous-phase Bell 103 audio (mark 2225 / space 2025 Hz, 300 baud)
    with a short idle-mark lead-in and tail, as between real bursts."""
    bit_n = _FS / 300.0
    bits = [1] * 12 + bits + [1] * 12
    freq = np.empty(int(round(len(bits) * bit_n)))
    for i, b in enumerate(bits):
        freq[int(round(i * bit_n)) : int(round((i + 1) * bit_n))] = 2225.0 if b else 2025.0
    audio = 0.5 * np.sin(2 * np.pi * np.cumsum(freq) / _FS)
    rng = np.random.default_rng(1)
    return (audio + rng.standard_normal(len(audio)) * noise_sigma).astype(np.float32)


def _chu():
    return ChuDecoder(clock=_CLOCK_UTC.timestamp)


def test_chu_decodes_a_burst_and_reports_once_per_minute():
    bursts = [encode_chu_burst(datetime(2026, 9, 25, 20, 31, s)) for s in (32, 33)]
    audio = np.concatenate([_fsk(chu_burst_bits(b), 0.15) for b in bursts])
    (event,) = _feed_all(_chu(), audio)
    assert event.decoder == "chu"
    assert event.fields["utc"] == "2026-09-25T20:31:32+00:00"
    assert event.integrity == "passed"


def test_chu_b_burst_supplies_dut1_but_not_the_year_to_the_next_a_burst():
    b = encode_chu_b_burst(2025, dut1_s=-0.3, tai_utc=37)
    a = encode_chu_burst(datetime(2026, 9, 25, 20, 31, 32))
    audio = np.concatenate([_fsk(chu_burst_bits(x)) for x in (b, a)])
    (event,) = _feed_all(_chu(), audio)
    # The year is the host clock's: B's layout is unverified off air.
    assert not event.fields["utc"].startswith("2025-")
    assert event.fields["utc"].endswith("-09-25T20:31:32+00:00")
    assert event.fields["dut1"] == -0.3 and event.fields["tai_utc"] == 37


def test_chu_rejects_a_burst_whose_halves_differ():
    burst = bytearray(encode_chu_burst(datetime(2026, 9, 25, 20, 31, 32)))
    burst[7] ^= 0x10
    assert _feed_all(_chu(), _fsk(chu_burst_bits(bytes(burst)))) == []


def test_chu_no_event_on_noise():
    assert _feed_all(_chu(), _noise(5, 0.3)) == []
