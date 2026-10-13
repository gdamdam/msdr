import asyncio

import pytest

from msdr.decoder.pydecoders.rs41 import RS41Backend
from tests.decoder.rs41_fixtures import (
    rs41_extended_test_frame,
    rs41_iq,
    rs41_test_frame,
)


@pytest.mark.parametrize("leading_samples", [0, 1, 5, 11, 12, 24, 33])
async def test_rs41_preamble_and_sync_acquire_at_integer_sample_offsets(leading_samples):
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    await backend.start()
    backend.feed(rs41_iq(rs41_test_frame(), leading_samples=leading_samples))
    event = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    assert event.integrity == "passed"
    assert event.fields["serial"] == "K1234567"


@pytest.mark.parametrize("offset_hz", [-9_000, -3_000, 3_000, 6_000, 12_000])
async def test_rs41_decodes_a_sonde_off_the_tuned_frequency(offset_hz):
    # Offsets beyond the 2.4 kHz deviation used to bias every symbol one way.
    from msdr.validation.impairments import frequency_offset
    from msdr.validation.scorecard import _iq_to_sc16, _sc16_to_iq

    iq = frequency_offset(
        _sc16_to_iq(rs41_iq(rs41_test_frame(), leading_samples=5)), offset_hz, 57_600.0
    )
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    await backend.start()
    backend.feed(_iq_to_sc16(iq))
    event = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    assert event.integrity == "passed"
    assert event.fields["serial"] == "K1234567"


@pytest.mark.parametrize("chunk_size", [4, 127 * 4, 4096 * 4])
async def test_rs41_timing_and_symbol_state_survive_feed_chunk_boundaries(chunk_size):
    iq = rs41_iq(rs41_test_frame(), leading_samples=5)
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    await backend.start()
    for offset in range(0, len(iq), chunk_size):
        backend.feed(iq[offset : offset + chunk_size])
    event = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    assert event.integrity == "passed"
    assert event.fields["frame_number"] == 321


@pytest.mark.parametrize("damaged_position", [56, 61])
async def test_iq_fec_repairs_frame_marker_and_payload_symbols(damaged_position):
    frame = bytearray(rs41_test_frame())
    frame[damaged_position] ^= 1
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    await backend.start()
    backend.feed(rs41_iq(bytes(frame), leading_samples=5))
    event = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    assert event.integrity == "passed"
    assert event.fields["serial"] == "K1234567"
    assert event.fields["rs_corrected_symbols"] == 1


async def test_extended_frame_is_held_until_518_bytes_then_decoded():
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    await backend.start()
    backend.feed(rs41_iq(rs41_extended_test_frame(), leading_samples=5))
    event = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    assert event.integrity == "passed"
    assert event.fields["frame_length"] == 518


async def test_consecutive_standard_frames_in_one_feed_emit_once_each():
    frame = rs41_test_frame()
    backend = RS41Backend(
        None, sample_rate_hz=57_600, center_hz=403_000_000, frequency_hz=403_000_000
    )
    await backend.start()
    backend.feed(rs41_iq(frame, leading_samples=5) + rs41_iq(frame))
    first = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    second = await asyncio.wait_for(backend.get_event(), timeout=0.5)
    assert first.fields["frame_number"] == second.fields["frame_number"] == 321
    assert backend._events.empty()
