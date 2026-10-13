"""Deterministic RS41 frame and IQ builders for decoder tests."""

from __future__ import annotations

import numpy as np

from msdr.decoder.payloads import _crc16_ccitt

_PRIMITIVE = 0x11D
_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _power in range(255):
    _EXP[_power] = _value
    _LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= _PRIMITIVE
for _power in range(255, len(_EXP)):
    _EXP[_power] = _EXP[_power - 255]


def _mul(a: int, b: int) -> int:
    return 0 if not a or not b else _EXP[_LOG[a] + _LOG[b]]


def _generator() -> list[int]:
    """Build the RS(255,231) generator with roots alpha^0..alpha^23."""
    polynomial = [1]
    for root in range(24):
        factor = _EXP[root]
        result = [0] * (len(polynomial) + 1)
        for index, value in enumerate(polynomial):
            result[index] ^= _mul(value, factor)
            result[index + 1] ^= value
        polynomial = result
    return polynomial


_GENERATOR = _generator()
_SYNC = bytes.fromhex("10b6ca11229612f8")
_DESCRAMBLE = bytes(
    (
        0x96,
        0x83,
        0x3E,
        0x51,
        0xB1,
        0x49,
        0x08,
        0x98,
        0x32,
        0x05,
        0x59,
        0x0E,
        0xF9,
        0x44,
        0xC6,
        0x26,
        0x21,
        0x60,
        0xC2,
        0xEA,
        0x79,
        0x5D,
        0x6D,
        0xA1,
        0x54,
        0x69,
        0x47,
        0x0C,
        0xDC,
        0xE8,
        0x5C,
        0xF1,
        0xF7,
        0x76,
        0x82,
        0x7F,
        0x07,
        0x99,
        0xA2,
        0x2C,
        0x93,
        0x7C,
        0x30,
        0x63,
        0xF5,
        0x10,
        0x2E,
        0x61,
        0xD0,
        0xBC,
        0xB4,
        0xB6,
        0x06,
        0xAA,
        0xF4,
        0x23,
        0x78,
        0x6E,
        0x3B,
        0xAE,
        0xBF,
        0x7B,
        0x4C,
        0xC1,
    )
)


def _encode_codeword(message: bytes) -> bytes:
    if len(message) != 231:
        raise ValueError("RS41 codeword message must be 231 bytes")
    work = [0] * 24 + list(message)
    for index in range(len(work) - 1, 23, -1):
        factor = work[index]
        for offset, coefficient in enumerate(_GENERATOR):
            work[index - 24 + offset] ^= _mul(factor, coefficient)
    return bytes(work[:24]) + message


def rs41_test_frame() -> bytes:
    """Return a CRC-valid 320-byte standard frame with valid RS parity."""
    status = bytearray(0x18)
    status[0:2] = (321).to_bytes(2, "little")
    status[2:10] = b"K1234567"
    status[0x0A] = 28
    gps = bytearray(0x13)
    gps[0:4] = (637813700).to_bytes(4, "little", signed=True)
    gps[0x0C:0x12] = b"\x64\x00\x9c\xff\x32\x00"
    gps[0x12] = 8

    def block(block_id: int, payload: bytes) -> bytes:
        checksum = _crc16_ccitt(payload).to_bytes(2, "little")
        return bytes((block_id, len(payload))) + payload + checksum

    blocks = block(0x79, bytes(status)) + block(0x7B, bytes(gps))
    remainder = 320 - 0x39 - len(blocks) - 4
    frame = bytearray(bytes(0x38) + b"\x0f" + blocks + block(0x76, bytes(remainder)))
    if len(frame) != 320:
        raise AssertionError("invalid test frame size")
    # Sync replaces the first eight on-air bytes; descrambling the inserted
    # sync must reconstruct the corresponding received bytes.
    frame[:8] = bytes(a ^ b for a, b in zip(_SYNC, _DESCRAMBLE[:8], strict=True))
    padded = frame.ljust(518, b"\x00")
    for lane in range(2):
        message = bytes(padded[56 + 2 * index + lane] for index in range(231))
        codeword = _encode_codeword(message)
        frame[8 + lane * 24 : 32 + lane * 24] = codeword[:24]
    return bytes(frame)


def rs41_bad_crc_frame() -> bytes:
    """Return an RS-valid frame whose first payload block CRC is invalid."""
    frame = bytearray(rs41_test_frame())
    frame[0x39 + 24 + 2] ^= 1  # First block's little-endian CRC.
    padded = frame.ljust(518, b"\x00")
    for lane in range(2):
        message = bytes(padded[56 + 2 * index + lane] for index in range(231))
        codeword = _encode_codeword(message)
        frame[8 + lane * 24 : 32 + lane * 24] = codeword[:24]
    return bytes(frame)


def rs41_extended_test_frame() -> bytes:
    """Return a CRC-valid 518-byte extended frame with valid RS parity."""
    standard = rs41_test_frame()

    def block(block_id: int, size: int) -> bytes:
        payload = bytes(size)
        checksum = _crc16_ccitt(payload).to_bytes(2, "little")
        return bytes((block_id, size)) + payload + checksum

    # Reuse the known status/GPS blocks and fill the extended-frame body with
    # bounded sub-blocks (the length field is one byte).
    blocks = standard[57 : 57 + 28 + 23] + block(0x76, 255) + block(0x77, 147)
    frame = bytearray(standard[:56] + b"\xf0" + blocks)
    if len(frame) != 518:
        raise AssertionError("invalid extended test frame size")
    padded = frame.ljust(518, b"\x00")
    for lane in range(2):
        message = bytes(padded[56 + 2 * index + lane] for index in range(231))
        codeword = _encode_codeword(message)
        frame[8 + lane * 24 : 32 + lane * 24] = codeword[:24]
    return bytes(frame)


def rs41_iq(frame: bytes, *, leading_samples: int = 0, amplitude: int = 24000) -> bytes:
    """Modulate one standard frame as deterministic 57.6 ksample/s GFSK IQ."""
    if len(frame) not in (320, 518):
        raise ValueError("expected a standard 320-byte or extended 518-byte RS41 frame")
    wire = _SYNC + bytes(
        value ^ _DESCRAMBLE[index & 63] for index, value in enumerate(frame[8:], 8)
    )
    bits = [index & 1 for index in range(200)]
    bits.extend((byte >> bit) & 1 for byte in wire for bit in range(8))
    frequencies = np.repeat(np.where(bits, 2400.0, -2400.0), 12)
    phase = 2 * np.pi * np.cumsum(frequencies) / 57600
    iq = np.exp(1j * phase).astype(np.complex64)
    if leading_samples:
        iq = np.concatenate((np.ones(leading_samples, np.complex64), iq))
    # Drain the production resampler's symmetric FIR tail so frame-end bits
    # are not discarded by an otherwise valid finite recording.
    iq = np.concatenate((iq, np.full(48, iq[-1], np.complex64)))
    values = np.column_stack((iq.real, iq.imag)) * amplitude
    return np.clip(np.round(values), -32768, 32767).astype("<i2").tobytes()
