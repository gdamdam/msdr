import asyncio
import socket

import pytest

from msdr.feeds.nmea import NmeaUdpFeed, armour_payload, format_aivdm, nmea_checksum

# Type 1 position report from the AIVDM/AIVDO protocol reference (gpsd docs).
KNOWN = "!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C"


def _frame_from_armoured(payload: str) -> bytes:
    bits = "".join(f"{(ord(c) - 48 if ord(c) < 88 else ord(c) - 56):06b}" for c in payload)
    bits = bits[: len(bits) // 8 * 8]
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def test_checksum_matches_known_sentence():
    body = KNOWN[1:KNOWN.index("*")]
    assert nmea_checksum(body) == "5C"


def test_armour_roundtrip_and_fill_bits():
    payload = KNOWN.split(",")[5]
    frame = _frame_from_armoured(payload)  # 168 bits
    assert armour_payload(frame) == (payload, 0)
    # A 7-byte frame is 56 bits: 10 chars with 4 fill bits.
    chars, fill = armour_payload(b"\x00" * 7)
    assert (len(chars), fill) == (10, 4)


def test_single_sentence_matches_reference_up_to_channel():
    frame = _frame_from_armoured(KNOWN.split(",")[5])
    [sentence] = format_aivdm(frame, channel="B")
    assert sentence == KNOWN


def test_multi_fragment_sentences_are_valid():
    frame = bytes(range(53))  # 424 bits: a type-5-sized frame -> 71 chars
    sentences = format_aivdm(frame, seq_id=3)
    assert len(sentences) == 2
    assert sentences[0].startswith("!AIVDM,2,1,3,A,")
    assert sentences[1].startswith("!AIVDM,2,2,3,A,")
    for sentence in sentences:
        assert len(sentence) <= 80
        body, _, cs = sentence[1:].partition("*")
        assert nmea_checksum(body) == cs
    payloads = "".join(s.split(",")[5] for s in sentences)
    assert payloads == armour_payload(frame)[0]
    assert sentences[0].split(",")[6].startswith("0")  # fill only on the last fragment
    assert sentences[1].split(",")[6].split("*")[0] == str(armour_payload(frame)[1])


@pytest.mark.asyncio
async def test_udp_send_to_local_socket():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    feed = NmeaUdpFeed("127.0.0.1", rx.getsockname()[1])
    await feed.start()
    try:
        feed.publish_frame(_frame_from_armoured(KNOWN.split(",")[5]))
        await asyncio.sleep(0.05)
        data = rx.recv(2048)
        assert data.startswith(b"!AIVDM,1,1,,A,177KQJ5000G?tO`K>RA1wUbN0TKH,0*")
        assert data.endswith(b"\r\n")
        assert feed.status()["sent"] == 1
    finally:
        await feed.stop()
        rx.close()


@pytest.mark.asyncio
async def test_runtime_forwards_ais_events():
    from msdr.feeds.runtime import FeedsRuntime
    from msdr.settings import FeedsSettings

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    runtime = FeedsRuntime(FeedsSettings(nmea_enabled=True, nmea_port=rx.getsockname()[1]))
    await runtime.start()
    try:
        frame = _frame_from_armoured(KNOWN.split(",")[5])
        runtime.on_message({"type": "decoder_event", "event": {
            "decoder": "ais", "integrity": "passed", "fields": {"payload_hex": frame.hex()}}})
        await asyncio.sleep(0.05)
        assert rx.recv(2048).startswith(b"!AIVDM,1,1,,A,")
    finally:
        await runtime.stop()
        rx.close()
