"""Synthetic Orbcomm SDPSK round trips (own modulator -> OrbcommBackend). Synthetic only."""

import asyncio
import math

import numpy as np

from msdr.decoder.pydecoders.orbcomm import (
    _SPS,
    _SYNC,
    OrbcommBackend,
    fletcher16,
    parse_frame,
    rrc_taps,
)

_RATE = 57_600


def make_body(ptype, sat, payload=b"", *, corrupt=False) -> bytes:
    body = bytes([ptype, sat]) + payload.ljust(68, b"\0")
    ck = fletcher16(body)
    if corrupt:
        ck ^= 0x0101
    return body + ck.to_bytes(2, "big")


def ephemeris_payload(lat_deg, lon_deg, alt_m) -> bytes:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    a, e2 = 6378137.0, 6.69437999014e-3
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1 - e2) + alt_m) * math.sin(lat)
    return b"".join(int(round(v)).to_bytes(4, "big", signed=True) for v in (x, y, z))


def frame_bits(body: bytes) -> list[int]:
    sync = [(_SYNC >> (23 - i)) & 1 for i in range(24)]
    return sync + [(byte >> (7 - i)) & 1 for byte in body for i in range(8)]


def modulate(bits, carrier_hz, *, noise=0.0, invert=False, lead=40, seed=0) -> bytes:
    """Differential BPSK (bit 1 = phase flip), RRC-shaped at 12 sps, int16 IQ."""
    rng = np.random.default_rng(seed)
    bits = list(rng.integers(0, 2, lead)) + list(bits) + list(rng.integers(0, 2, lead))
    sym, state = [1.0], 1.0
    for b in bits:
        state = -state if (b ^ invert) else state
        sym.append(state)
    up = np.zeros(len(sym) * _SPS)
    up[::_SPS] = sym
    base = np.convolve(up, rrc_taps(), mode="same")
    t = np.arange(len(base)) / _RATE
    sig = 0.4 * base * np.exp(2j * np.pi * carrier_hz * t + 0.9j)
    sig = sig + noise * (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig)))
    iq = np.empty(2 * len(sig), np.int16)
    iq[0::2], iq[1::2] = (np.clip(sig.real, -1, 1) * 32767), (np.clip(sig.imag, -1, 1) * 32767)
    return iq.tobytes()


def run(data: bytes, *, offset_hz=0.0, chunk=4096) -> list:
    async def go():
        dec = OrbcommBackend(sample_rate_hz=_RATE, center_hz=0.0, frequency_hz=offset_hz)
        await dec.start()
        for i in range(0, len(data), chunk):
            dec.feed(data[i : i + chunk])
        await dec.stop()
        return [e async for e in dec.events()]

    return asyncio.run(go())


def test_fletcher_and_parse_reject_bad_checksum():
    good = make_body(0x02, 7, b"hello")
    assert parse_frame(good)["packet_type"] == "message"
    assert parse_frame(make_body(0x02, 7, b"hello", corrupt=True)) is None
    assert parse_frame(make_body(0x7F, 7)) is None  # unknown type, valid checksum


def test_round_trip_all_packet_types_with_offset_and_noise():
    bodies = [
        make_body(0x01, 3, b"\x01\x02"),
        make_body(0x07, 3, ephemeris_payload(41.5, -73.25, 745_000)),
        make_body(0x02, 3, b"\x05hello"),
        make_body(0x03, 3),
        make_body(0x04, 3),
        make_body(0x05, 3),
        make_body(0x06, 0),
    ]
    bits = [b for body in bodies for b in frame_bits(body)]
    # 2300 Hz Doppler-ish error on top of a 5 kHz channel offset.
    data = modulate(bits, 5_000 + 2_300, noise=0.08)
    events = run(data, offset_hz=5_000)
    types = [e.fields["packet_type"] for e in events]
    assert types == [
        "sync", "ephemeris", "message", "uplink_channel", "downlink_channel", "network", "fill",
    ]
    eph = events[1].fields
    assert eph["satellite_id"] == 3
    assert abs(eph["lat"] - 41.5) < 0.01 and abs(eph["lon"] + 73.25) < 0.01
    assert abs(eph["altitude_m"] - 745_000) < 100
    assert "satellite_id" not in events[-1].fields
    assert all(e.decoder == "orbcomm" and e.integrity == "passed" for e in events)


def test_polarity_inverted_stream_decodes():
    bits = frame_bits(make_body(0x02, 9, b"\x03abc"))
    events = run(modulate(bits, -1_800, noise=0.08, invert=True, seed=3))
    assert [e.fields["packet_type"] for e in events] == ["message"]


def test_bad_checksum_frame_is_not_emitted():
    bits = frame_bits(make_body(0x02, 7, b"x", corrupt=True)) + frame_bits(make_body(0x01, 7))
    events = run(modulate(bits, 1_000, noise=0.05, seed=5))
    assert [e.fields["packet_type"] for e in events] == ["sync"]


def test_noise_only_emits_nothing():
    rng = np.random.default_rng(11)
    n = _RATE * 3
    iq = (rng.standard_normal(2 * n) * 4000).astype(np.int16)
    assert run(iq.tobytes()) == []


def test_ephemeris_implausible_position_is_not_mapped():
    # Radius ~ 1000 km altitude is plausible; a zero vector is not.
    fields = parse_frame(make_body(0x07, 1, b"\0" * 12))
    assert "lat" not in fields and fields["position_ecef_m"] == [0, 0, 0]
