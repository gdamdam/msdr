"""AisDecoder on synthetic GMSK 9600 bursts (tests/helpers/gmsk.py).

SYNTHETIC ONLY: frame-success numbers here are a regression bar for the
discriminator path, not a claim about off-air performance (field validation
pending -- see capability_matrix MATRIX["ais"])."""

import numpy as np

from msdr.decoder.pydecoders.ais import AisDecoder, crc16_x25, decode_payload
from tests.helpers import gmsk

FS = 48_000
OFFSET_HZ = 600.0
PAD = np.zeros(200, dtype=complex)


def _decode(flat: np.ndarray, chunk: int | None = None) -> tuple[list, AisDecoder]:
    dec = AisDecoder()
    events = []
    if chunk is None:
        events += dec.feed(flat)
    else:
        for i in range(0, flat.size, chunk):
            events += dec.feed(flat[i : i + chunk])
    events += dec.flush()
    return events, dec


def _burst_flat(payload: bytes, ebn0_db: float, rng, **kw) -> np.ndarray:
    sig = np.concatenate((PAD, gmsk.make_burst(payload, FS, rng, **kw), PAD))
    sig = gmsk.rotate(sig, OFFSET_HZ, rng.uniform(0, 2 * np.pi), FS)
    return gmsk.iq_to_flat(gmsk.add_noise(sig, ebn0_db, FS, rng))


def test_crc16_x25_check_value():
    assert crc16_x25(b"123456789") == 0x906E


def test_type1_payload_fields_decode_exactly():
    rng = np.random.default_rng(11)
    payload = gmsk.ais_type1(
        366123456,
        nav_status=5,
        sog_tenths=123,
        lon_deg=-122.4194,
        lat_deg=37.8081,
        cog_tenths=2710,
        heading=270,
    )
    events, dec = _decode(_burst_flat(payload, 25.0, rng))
    assert len(events) == 1
    ev = events[0]
    assert ev.decoder == "ais" and ev.kind == "text"
    f = ev.fields
    assert f["type"] == 1
    assert f["mmsi"] == 366123456
    assert f["nav_status"] == 5
    assert f["sog_kn"] == 12.3
    assert f["lon_deg"] == -122.4194
    assert f["lat_deg"] == 37.8081
    assert f["cog_deg"] == 271.0
    assert f["heading_deg"] == 270
    assert "366123456" in ev.text
    # Present once W2's DecodeEvent integrity fields land; absent before.
    assert getattr(ev, "integrity", "passed") == "passed"
    assert getattr(ev, "integrity_note", "CRC-16/X.25 verified") == "CRC-16/X.25 verified"
    assert dec.stats()["frames_ok"] == 1


def test_not_available_sentinels_decode_as_none():
    payload = gmsk.ais_type1(
        1, sog_tenths=1023, lon_deg=181.0, lat_deg=91.0, cog_tenths=3600, heading=511
    )
    f = decode_payload(payload)
    assert f["mmsi"] == 1
    assert f["sog_kn"] is None
    assert f["lon_deg"] is None
    assert f["lat_deg"] is None
    assert f["cog_deg"] is None
    assert f["heading_deg"] is None


def test_type18_position_fields():
    payload = gmsk.pack_bits(
        [
            (18, 6),
            (0, 2),
            (338000111, 30),
            (0, 8),  # reserved
            (55, 10),  # SOG 5.5 kn
            (0, 1),
            (round(-70.25 * 600_000), 28),
            (round(41.5 * 600_000), 27),
            (900, 12),  # COG 90.0
            (91, 9),
            (0, 6),
            (0, 29),  # remaining flags/radio
        ]
    )
    f = decode_payload(payload)
    assert (f["type"], f["mmsi"]) == (18, 338000111)
    assert f["sog_kn"] == 5.5
    assert f["lon_deg"] == -70.25
    assert f["lat_deg"] == 41.5
    assert f["cog_deg"] == 90.0
    assert f["heading_deg"] == 91
    assert "nav_status" not in f


def test_type5_and_24_carry_only_type_and_mmsi():
    for msg_type in (5, 24):
        payload = gmsk.pack_bits([(msg_type, 6), (0, 2), (211222333, 30), (0, 130)])
        f = decode_payload(payload)
        assert (f["type"], f["mmsi"]) == (msg_type, 211222333)
        assert not {"sog_kn", "lat_deg", "lon_deg", "nav_status"} & set(f)


def test_crc_failure_is_counted_not_emitted():
    rng = np.random.default_rng(12)
    events, dec = _decode(_burst_flat(gmsk.ais_type1(1234), 25.0, rng, corrupt_crc=True))
    assert events == []
    stats = dec.stats()
    assert stats["crc_fail"] >= 1
    assert stats["frames_ok"] == 0


def test_crc_valid_frame_without_mmsi_is_not_emitted():
    rng = np.random.default_rng(13)
    events, dec = _decode(_burst_flat(b"\x04\x00\x01", 25.0, rng))
    assert events == []
    assert dec.stats()["no_mmsi"] == 1


def test_frame_success_by_ebn0():
    """200 bursts per Eb/N0 point (random 168-bit payloads, as the PoC).
    Prints the table (pytest -s) that capability_matrix MATRIX["ais"] quotes."""
    rng = np.random.default_rng(20260927)
    table = {}
    for ebn0 in (10, 12, 14, 16, 20):
        ok = 0
        for _ in range(200):
            payload = gmsk.random_payload(rng)
            events, _ = _decode(_burst_flat(payload, ebn0, rng))
            ok += any(e.fields["payload_hex"] == payload.hex() for e in events)
        table[ebn0] = ok / 200
    print("AIS frame success (synthetic):", table)
    assert table[14] >= 0.85
    assert table[16] >= 0.95
    assert table[20] >= 0.99
    assert table[10] <= table[12] <= table[14] <= table[16] <= table[20]


def test_noise_only_has_no_crc_false_passes():
    rng = np.random.default_rng(14)
    noise = gmsk.add_noise(np.zeros(10 * FS, dtype=complex), 0.0, FS, rng)
    events, dec = _decode(gmsk.iq_to_flat(noise), chunk=1024)
    assert events == []
    assert dec.stats()["frames_ok"] == 0


def _burst_train(rng, seconds: float = 3.0, ebn0_db: float = 20.0):
    total = int(FS * seconds)
    stream = np.zeros(total, dtype=complex)
    slot = int(FS * 0.02667)
    sent = []
    for start in range(slot, total - 2 * slot, 4 * slot):
        payload = gmsk.random_payload(rng)
        sig = gmsk.make_burst(payload, FS, rng)
        stream[start : start + sig.size] += gmsk.rotate(
            sig, OFFSET_HZ, rng.uniform(0, 2 * np.pi), FS, start
        )
        sent.append(payload.hex())
    return gmsk.iq_to_flat(gmsk.add_noise(stream, ebn0_db, FS, rng)), sent


def test_streamed_chunks_decode_every_burst_once():
    flat, sent = _burst_train(np.random.default_rng(15))
    events, _ = _decode(flat, chunk=1000)
    got = [e.fields["payload_hex"] for e in events]
    assert sorted(got) == sorted(sent)  # every burst, no duplicates across chunks


def test_decoder_is_input_scale_free():
    flat, sent = _burst_train(np.random.default_rng(16), seconds=1.0)
    for gain in (0.05, 1.0, 20.0):
        events, _ = _decode(flat * gain, chunk=4800)
        assert sorted(e.fields["payload_hex"] for e in events) == sorted(sent), gain
