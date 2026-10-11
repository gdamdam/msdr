"""Synthesised one-minute-frame round trips for the S7-S10 time-code
decoders (round 2): WWV/WWVH, DCF77, MSF.

Widths below must match each decoder's own classify_*_pulse() bands in
msdr/decoder/pydecoders/timecode.py (same relationship as test_pydecoders.py's
navtex section re-declaring _MARK_HZ/_SPACE_HZ/_BAUD next to the production
module, rather than importing them)."""

from datetime import UTC, datetime

import numpy as np

from msdr.decoder.pydecoders.timecode import (
    Dcf77Decoder,
    MsfDecoder,
    WwvDecoder,
    encode_dcf77_minute,
    encode_msf_minute,
    encode_wwv_minute,
)

_FS = 11025
_CARRIER_HZ = 400.0

# Every decoder's *first-ever* pulse is unmeasurable (its start precedes the
# stream the decoder ever saw) -- true of any freshly constructed decoder,
# self-correcting one pulse later in real continuous operation. A lead-in of
# steady "no pulse yet" audio before the frame under test gives that one
# throwaway pulse somewhere harmless instead of eating bit 0 of the frame.
_LEAD_IN_S = 1.5


def _render_on_pulses(labels: list[str], nominal_s: dict[str, float], tone_hz: float) -> np.ndarray:
    """WWV-style: each second is `nominal_s[label]` of a tone at full
    amplitude (the "on" subcarrier burst) followed by silence."""
    chunks = [np.zeros(int(round(_LEAD_IN_S * _FS)))]
    for lbl in labels:
        n_on = int(round(nominal_s[lbl] * _FS))
        n_off = _FS - n_on
        chunks.append(np.ones(n_on))
        chunks.append(np.zeros(n_off))
    env = np.concatenate(chunks)
    t = np.arange(len(env)) / _FS
    return (env * np.sin(2 * np.pi * tone_hz * t)).astype(np.float32)


def _render_off_pulses(
    labels: list[str], nominal_s: dict[str, float], tone_hz: float, *, gap_and_next: bool = False
) -> np.ndarray:
    """DCF77/MSF-style: each second is full-amplitude carrier, reduced to a
    low level for `nominal_s[label]` (the "drop") at the start of the
    second.

    `gap_and_next`: DCF77 only -- appends a full second of undropped carrier
    (the real second-59 "no drop" minute marker) followed by one more,
    otherwise-throwaway, normal dwell (the next minute's bit 0) so the
    decoder's gap-detector has something to close the frame against."""
    chunks = [np.ones(int(round(_LEAD_IN_S * _FS)))]
    for lbl in labels:
        n_low = int(round(nominal_s[lbl] * _FS))
        n_high = _FS - n_low
        chunks.append(np.full(n_low, 0.05))
        chunks.append(np.ones(n_high))
    if gap_and_next:
        chunks.append(np.ones(_FS))  # second 59: no drop
        n_low = int(round(nominal_s["0"] * _FS))
        chunks.append(np.full(n_low, 0.05))
        chunks.append(np.ones(_FS - n_low))
    env = np.concatenate(chunks)
    t = np.arange(len(env)) / _FS
    return (env * np.sin(2 * np.pi * tone_hz * t)).astype(np.float32)


def _feed_all(decoder, audio: np.ndarray, chunk: int = 1102) -> list:
    events = []
    for i in range(0, len(audio), chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    return events


def _clock_at_end(audio: np.ndarray, host_epoch: float = 1_700_000_000.0):
    """A fake host clock whose value, when the decoder asks for "now" at the
    end of the run, is exactly `host_epoch` seconds after the audio started
    -- makes offset_s = host_epoch_as_utc - decoded_minute_start exactly
    checkable."""
    return lambda: host_epoch + len(audio) / _FS


# -- WWV/WWVH -----------------------------------------------------------


_WWV_NOMINAL_S = {"0": 0.170, "1": 0.470, "M": 0.770}


def test_wwv_decodes_one_minute_frame():
    dt = datetime(2026, 9, 25, 20, 31, 0, tzinfo=UTC)
    labels = encode_wwv_minute(dt, dut1_s=-0.3, leap=True)
    audio = _render_on_pulses(labels, _WWV_NOMINAL_S, 100.0)

    host_epoch = 1_700_000_000.0
    dec = WwvDecoder(clock=_clock_at_end(audio, host_epoch))
    events = _feed_all(dec, audio)

    assert len(events) == 1
    event = events[0]
    assert event.kind == "time"
    assert event.fields["utc"] == dt.isoformat()
    assert event.fields["station"] == "WWV/WWVH"
    assert event.fields["leap"] is True
    assert abs(event.fields["dut1"] - (-0.3)) < 1e-9
    assert event.fields["quality"] > 0.9
    # Tolerance covers the 100 Hz band-pass filter's own rise time (its
    # narrow 15 Hz half-bandwidth means several tens of ms) stacked on the
    # envelope smoothing/group delay -- the documented fixed offset_s bias.
    expected_offset = host_epoch + _LEAD_IN_S - dt.timestamp()
    assert abs(event.fields["offset_s"] - expected_offset) < 0.2


def test_wwv_ambiguous_pulse_widths_lower_quality():
    dt = datetime(2026, 9, 25, 20, 31, 0, tzinfo=UTC)
    labels = encode_wwv_minute(dt)
    # A borderline width (roughly midway between the "0" and "1" bands)
    # still classifies but with reduced per-pulse confidence.
    nominal = dict(_WWV_NOMINAL_S)
    audio = _render_on_pulses(labels, nominal, 100.0)
    # Corrupt the rendered width of one data pulse (not a marker) directly by
    # re-rendering with a skewed width for that one second's label.
    skewed = {"0": 0.170, "1": 0.35, "M": 0.770}  # "1" pushed toward its band edge
    audio_skewed = _render_on_pulses(labels, skewed, 100.0)

    dec_clean = WwvDecoder(clock=_clock_at_end(audio))
    q_clean = _feed_all(dec_clean, audio)[0].fields["quality"]

    dec_skewed = WwvDecoder(clock=_clock_at_end(audio_skewed))
    events = _feed_all(dec_skewed, audio_skewed)
    assert len(events) == 1  # still recoverable -- just lower confidence
    assert events[0].fields["quality"] < q_clean


def test_wwv_no_event_on_noise():
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(_FS * 10) * 0.01).astype(np.float32)
    dec = WwvDecoder()
    assert _feed_all(dec, noise) == []


# -- DCF77 ----------------------------------------------------------------

_DCF77_NOMINAL_S = {"0": 0.100, "1": 0.200}


def test_dcf77_decodes_one_minute_frame_cest_to_utc():
    local_dt = datetime(2026, 9, 25, 22, 31, 0)  # CEST (+2h) -> UTC 20:31
    labels = encode_dcf77_minute(local_dt, dst=True, leap=False)
    # DCF77 frames on a *missing* pulse (second 59), so closing out the
    # frame needs the following minute's first pulse to arrive too.
    audio = _render_off_pulses(labels, _DCF77_NOMINAL_S, _CARRIER_HZ, gap_and_next=True)

    host_epoch = 1_700_000_000.0
    dec = Dcf77Decoder(clock=_clock_at_end(audio, host_epoch))
    events = _feed_all(dec, audio)

    assert len(events) == 1
    event = events[0]
    expected_utc = datetime(2026, 9, 25, 20, 31, 0, tzinfo=UTC)
    assert event.fields["utc"] == expected_utc.isoformat()
    assert event.fields["station"] == "DCF77"
    assert event.fields["dut1"] is None
    assert event.fields["leap"] is False
    assert event.fields["quality"] == 1.0


def test_dcf77_parity_error_lowers_quality_but_still_emits():
    local_dt = datetime(2026, 9, 25, 22, 31, 0)
    labels = encode_dcf77_minute(local_dt, dst=True, leap=False)
    corrupted = list(labels)
    # Flip the minute-parity bit itself: framing (start-of-time bit 20, the
    # missing-pulse marker) is untouched, only the minute field's parity
    # check fails.
    corrupted[28] = "1" if corrupted[28] == "0" else "0"

    audio = _render_off_pulses(corrupted, _DCF77_NOMINAL_S, _CARRIER_HZ, gap_and_next=True)

    dec = Dcf77Decoder(clock=_clock_at_end(audio))
    events = _feed_all(dec, audio)
    assert len(events) == 1
    assert events[0].fields["quality"] < 1.0


# -- MSF --------------------------------------------------------------------

_MSF_NOMINAL_S = {"00": 0.100, "10": 0.200, "01": 0.300, "11": 0.400, "M": 0.500}


def test_msf_decodes_one_minute_frame_bst_to_utc():
    local_dt = datetime(2026, 9, 25, 21, 31, 0)  # BST (+1h) -> UTC 20:31
    labels = encode_msf_minute(local_dt, dst=True)
    audio = _render_off_pulses(labels, _MSF_NOMINAL_S, _CARRIER_HZ)

    host_epoch = 1_700_000_000.0
    dec = MsfDecoder(clock=_clock_at_end(audio, host_epoch))
    events = _feed_all(dec, audio)

    assert len(events) == 1
    event = events[0]
    expected_utc = datetime(2026, 9, 25, 20, 31, 0, tzinfo=UTC)
    assert event.fields["utc"] == expected_utc.isoformat()
    assert event.fields["station"] == "MSF"
    assert event.fields["dut1"] is None
    assert event.fields["leap"] is None
    assert event.fields["quality"] == 1.0


def test_msf_parity_error_lowers_quality_but_still_emits():
    local_dt = datetime(2026, 9, 25, 21, 31, 0)
    labels = encode_msf_minute(local_dt, dst=True)
    corrupted = list(labels)
    # Flip the hour-parity bit (B channel of second 55): A-channel data (and
    # the minute-marker framing) is untouched.
    a, b = corrupted[55][0], corrupted[55][1]
    corrupted[55] = a + ("1" if b == "0" else "0")

    audio = _render_off_pulses(corrupted, _MSF_NOMINAL_S, _CARRIER_HZ)
    dec = MsfDecoder(clock=_clock_at_end(audio))
    events = _feed_all(dec, audio)
    assert len(events) == 1
    assert events[0].fields["quality"] < 1.0
