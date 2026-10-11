from msdr.decoder.pydecoders.rs41 import RS41Backend
from msdr.decoder.pydecoders.rs41_fec import correct_rs41_frame
from tests.decoder.rs41_fixtures import (
    _GENERATOR,
    rs41_bad_crc_frame,
    rs41_extended_test_frame,
    rs41_test_frame,
)


def test_clean_rs41_frame_passes_without_changes():
    frame = rs41_test_frame()
    assert correct_rs41_frame(frame) == (frame, 0)


def test_two_interleaved_codewords_correct_up_to_twelve_symbols_each():
    frame = bytearray(rs41_test_frame())
    # Independent published RS41 generator coefficients, written here in
    # ascending order to match the shortened codeword representation.
    assert _GENERATOR == [
        0x75,
        0x90,
        0xD9,
        0x7F,
        0xF7,
        0xED,
        0x01,
        0xCE,
        0x2B,
        0x3D,
        0x48,
        0x82,
        0x49,
        0xE5,
        0x96,
        0x73,
        0x66,
        0xD8,
        0xED,
        0xB2,
        0x46,
        0xA9,
        0x76,
        0x7A,
        0x01,
    ]
    # Twelve distinct parity symbols in each interleave lane is the RS(255,
    # 231) correction boundary, including the marker-bearing payload lane.
    positions = [*range(8, 20), *range(32, 44)]
    for index, position in enumerate(positions):
        frame[position] ^= index + 1
    corrected = correct_rs41_frame(bytes(frame))
    assert corrected == (rs41_test_frame(), 24)


def test_more_than_twelve_symbols_in_one_lane_is_rejected():
    frame = bytearray(rs41_test_frame())
    for index, position in enumerate(range(8, 21)):
        frame[position] ^= index + 1
    assert correct_rs41_frame(bytes(frame)) is None


def test_frame_shape_is_checked_and_damaged_marker_is_repaired():
    frame = rs41_test_frame()
    assert correct_rs41_frame(frame[:-1]) is None
    broken = bytearray(frame)
    broken[0x38] = 0
    assert correct_rs41_frame(bytes(broken)) == (frame, 1)


def test_crc_remains_the_final_gate_after_rs_correction():
    from msdr.decoder.pydecoders.rs41 import RS41Backend

    frame = rs41_bad_crc_frame()
    assert correct_rs41_frame(frame) == (frame, 0)
    backend = RS41Backend(None, sample_rate_hz=57_600, center_hz=1, frequency_hz=1)
    backend._emit_frame(frame)
    assert backend._events.empty()


def test_extended_frame_uses_all_231_symbols_per_lane():
    frame = rs41_extended_test_frame()
    assert len(frame) == 518
    assert correct_rs41_frame(frame) == (frame, 0)
    backend = RS41Backend(None, sample_rate_hz=57_600, center_hz=1, frequency_hz=1)
    backend._emit_frame(frame)
    assert backend._events.qsize() == 1
