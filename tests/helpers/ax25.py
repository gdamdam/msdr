"""Synthetic AX.25 / AFSK-1200 (Bell 202) test signals (parity fixture).

Frame -> bits -> tones -> FM IQ, all deterministic:

- AX.25 UI frame: 7-byte address fields (callsign shifted left one bit,
  SSID byte with the end-of-address bit on the source), control 0x03,
  PID 0xF0, info, then the CRC-16/X.25 FCS sent low byte first (the CRC used by
  the AIS GMSK burst generator, ``crc16_x25``).
- HDLC: LSB-first bits, a zero stuffed after five ones, 0x7E flags around
  the (stuffed) body, never stuffed themselves.
- NRZI: 0 = tone change, 1 = no change.
- Bell 202: mark (level 1) 1200 Hz, space (level 0) 2200 Hz, 1200 Bd,
  phase-continuous.
- FM: the AFSK audio frequency-modulates a carrier at ``offset_hz`` from the
  IQ centre with ``deviation_hz`` peak deviation.
"""

import numpy as np

BAUD = 1200
MARK_HZ = 1200.0
SPACE_HZ = 2200.0
FLAG = 0x7E


def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


assert crc16_x25(b"123456789") == 0x906E  # standard CRC-16/X.25 check value


def _address(call: str, *, last: bool) -> bytes:
    name, _, ssid = call.upper().partition("-")
    if not 1 <= len(name) <= 6:
        raise ValueError(f"callsign {call!r} must be 1-6 characters")
    out = bytes((ord(c) << 1) for c in name.ljust(6))
    ssid_byte = 0x60 | ((int(ssid or 0) & 0x0F) << 1) | (1 if last else 0)
    return out + bytes([ssid_byte])


def ui_frame(source: str, dest: str, info: str) -> bytes:
    """AX.25 UI frame (no digipeaters) *without* flags, FCS appended."""
    body = _address(dest, last=False) + _address(source, last=True) + b"\x03\xf0"
    body += info.encode("ascii")
    fcs = crc16_x25(body)
    return body + bytes([fcs & 0xFF, fcs >> 8])


def _lsb_bits(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def hdlc_bits(frame: bytes, *, preamble_flags: int = 32, tail_flags: int = 4) -> list[int]:
    """Flags + bit-stuffed frame + flags (frame already carries its FCS)."""
    stuffed, ones = [], 0
    for bit in _lsb_bits(frame):
        stuffed.append(bit)
        ones = ones + 1 if bit else 0
        if ones == 5:
            stuffed.append(0)
            ones = 0
    flag = _lsb_bits(bytes([FLAG]))
    return flag * preamble_flags + stuffed + flag * tail_flags


def nrzi(bits: list[int], level: int = 1) -> np.ndarray:
    out = np.empty(len(bits), dtype=np.int8)
    for i, bit in enumerate(bits):
        if bit == 0:
            level ^= 1
        out[i] = level
    return out


def afsk_tone_hz(levels: np.ndarray, fs: float) -> np.ndarray:
    """Per-sample instantaneous tone frequency (mark/space) at rate `fs`."""
    n = int(round(len(levels) * fs / BAUD))
    bit_of_sample = np.minimum((np.arange(n) * BAUD / fs).astype(np.int64), len(levels) - 1)
    return np.where(levels[bit_of_sample] == 1, MARK_HZ, SPACE_HZ)


def afsk_audio(levels: np.ndarray, fs: float, phase: float = 0.0) -> np.ndarray:
    tone = afsk_tone_hz(levels, fs)
    return np.sin(phase + 2 * np.pi * np.cumsum(tone) / fs).astype(np.float32)


def fm_iq(
    audio: np.ndarray,
    fs: float,
    *,
    offset_hz: float,
    deviation_hz: float = 3_000.0,
    amplitude: float = 0.25,
    noise_rms: float = 0.0,
    seed: int = 1,
) -> np.ndarray:
    """complex64: carrier at `offset_hz`, FM by `audio` (peak 1 -> deviation)."""
    n = np.arange(audio.size, dtype=np.float64)
    phase = 2 * np.pi * offset_hz * n / fs
    phase += 2 * np.pi * deviation_hz * np.cumsum(audio.astype(np.float64)) / fs
    iq = amplitude * np.exp(1j * phase)
    if noise_rms:
        rng = np.random.default_rng(seed)
        iq = iq + noise_rms / np.sqrt(2) * (
            rng.standard_normal(audio.size) + 1j * rng.standard_normal(audio.size)
        )
    return iq.astype(np.complex64)


def burst_train_audio(
    frames: list[tuple[str, str, str]], fs: float, *, lead_s: float = 0.5, gap_s: float = 0.4
) -> np.ndarray:
    """Audio at `fs`: silence (unmodulated carrier) lead-in, then each
    (source, dest, info) frame as one AFSK burst, `gap_s` of silence after each."""
    parts = [np.zeros(int(lead_s * fs), dtype=np.float32)]
    for source, dest, info in frames:
        levels = nrzi(hdlc_bits(ui_frame(source, dest, info)))
        parts.append(afsk_audio(levels, fs))
        parts.append(np.zeros(int(gap_s * fs), dtype=np.float32))
    return np.concatenate(parts)


def to_ci16(iq: np.ndarray) -> bytes:
    """complex -> interleaved little-endian int16 I/Q (SigMF ci16_le)."""
    inter = np.empty(iq.size * 2, dtype=np.float32)
    inter[0::2] = iq.real
    inter[1::2] = iq.imag
    return (np.clip(inter, -1.0, 1.0) * 32767).astype("<i2").tobytes()
