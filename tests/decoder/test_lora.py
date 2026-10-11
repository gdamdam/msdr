from types import SimpleNamespace

import pytest

from msdr.decoder.pydecoders.lora import (
    _decode_meshcore_packet,
    _decode_meshtastic_packet,
    _FrameAssembler,
)
from msdr.decoder.registry import DECODERS
from msdr.settings import DecoderSettings


def test_stream_assembler_handles_chunks_offsets_and_crc_status():
    assembler = _FrameAssembler()

    # The first two noise bytes precede an absolute-offset-tagged frame start.
    assert assembler.feed(b"??ab", {2: (4, True)}) == []
    assert assembler.feed(b"cd", {}) == [b"abcd"]

    # Two frames in a single scheduler buffer, with an invalid CRC on the first.
    assert assembler.feed(b"bad!good", {0: (4, False), 4: (4, True)}) == [b"good"]

    # An incomplete frame is abandoned at the next tagged sync, even if that
    # following frame has a valid CRC.
    assert assembler.feed(b"xxnew!", {0: (5, True), 2: (4, True)}) == [b"new!"]


def test_meshcore_header_fields_follow_v1_wire_layout_and_leave_body_opaque():
    # Version 1, text-message type, direct route, zero-hop path, body bytes.
    decoded = _decode_meshcore_packet(bytes([0x0A, 0x00]) + b"hello")

    assert decoded["protocol"] == "MeshCore"
    assert decoded["packet_type"] == "text_message"
    assert decoded["route_type"] == 2
    assert decoded["payload_version"] == 1
    assert decoded["path_hops"] == 0
    assert decoded["mesh_payload_hex"] == b"hello".hex()


def test_meshcore_transport_route_and_invalid_version_are_safe():
    decoded = _decode_meshcore_packet(bytes([0x00]) + b"\x01\x00\x02\x00" + bytes([0x00, 0xAA]))
    assert decoded["transport_codes_hex"] == "01000200"
    assert decoded["path_hops"] == 0

    future = _decode_meshcore_packet(bytes([0x4A, 0x00]) + b"payload")
    assert future["packet_type"] == "malformed_or_truncated"


def test_meshtastic_header_is_exposed_but_payload_is_not_guessed_as_plaintext():
    payload = (
        (0xFFFFFFFF).to_bytes(4, "little")
        + (42).to_bytes(4, "little")
        + (0x1234).to_bytes(4, "little")
        + bytes([0b00000101, 7, 3, 4])
        + b"looks like text"
    )
    decoded = _decode_meshtastic_packet(payload)

    assert decoded["packet_type"] == "mesh_packet"
    assert decoded["packet_id"] == "0x00001234"
    assert decoded["hop_limit"] == 5
    assert decoded["opaque"] is True
    assert decoded["encrypted"] is None
    assert decoded["text"].endswith("opaque)")


@pytest.mark.parametrize("bandwidth", [62500, 125000])
def test_live_backend_receives_persisted_profile_settings(bandwidth):
    from msdr.decoder.manager import _make_protocol_iq_backend

    profile = DecoderSettings(
        lora_spreading_factor=9,
        lora_bandwidth_hz=bandwidth,
        lora_coding_rate=2,
        lora_protocol="meshtastic",
    )
    backend = _make_protocol_iq_backend(
        DECODERS["lora"],
        sample_rate_hz=500000,
        center_hz=915000000,
        frequency_hz=915000000,
        decoder_settings=profile,
    )

    assert backend.spreading_factor == 9
    assert backend.bandwidth_hz == bandwidth
    assert backend.coding_rate == 2
    assert backend.sync_word == 0x2B

    from msdr.decoder.iq_backend import build_iq_backend

    replay_backend = build_iq_backend(
        DECODERS["lora"],
        {},
        sample_rate_hz=500000,
        center_hz=915000000,
        frequency_hz=915000000,
        decoder_settings=profile,
    )
    assert replay_backend.bandwidth_hz == bandwidth
    assert replay_backend.spreading_factor == 9
    assert replay_backend.sync_word == 0x2B


def test_native_lora_packet_metadata_uses_decoded_coding_rate():
    from msdr.decoder.pydecoders.lora import LoRaBackend

    backend = LoRaBackend(
        DECODERS["lora"], sample_rate_hz=250000, center_hz=915000000,
        frequency_hz=915000000, coding_rate=1,
    )
    backend._running = True
    backend._accept_packet(
        SimpleNamespace(crc_valid=True, payload=b"payload", cr=3, cfo_hz=0.0, snr_db=10.0)
    )
    event = backend._events.get_nowait()
    assert event.fields["coding_rate"] == "4/7"


def test_readiness_reports_gr_crosscheck_as_optional_and_missing(monkeypatch):
    """lora is builtin (the native receiver needs nothing external); the
    gr-lora_sdr cross-check backend is reported separately as optional, via
    install_hint, and never makes the decoder itself report "missing"."""
    from msdr.decoder.pydecoders import lora
    from msdr.decoder.readiness import installation_for

    monkeypatch.setattr(lora, "_gr_lora_available", lambda: False)
    installation = installation_for(DECODERS["lora"], {})

    assert installation.state == "builtin"
    assert "gr-lora_sdr" in installation.install_hint

    monkeypatch.setattr(lora, "_gr_lora_available", lambda: True)
    installation = installation_for(DECODERS["lora"], {})

    assert installation.state == "builtin"
    assert installation.install_hint == ""
