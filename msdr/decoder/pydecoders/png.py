"""Minimal 8-bit grayscale / RGB PNG writer -- zlib + struct only, no
Pillow/scipy (spec constraint: numpy only)."""

import os
import struct
import zlib

import numpy as np

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def encode_gray8(width: int, rows) -> bytes:
    """rows: sequence of bytes-like, each exactly `width` bytes (0-255 gray)."""
    height = len(rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # bit depth 8, color type 0
    raw = bytearray()
    for row in rows:
        raw.append(0)  # per-scanline filter type 0 (None)
        raw.extend(row)
    idat = zlib.compress(bytes(raw), 6)
    return _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def encode_rgb8(image: np.ndarray) -> bytes:
    """image: uint8 array of shape (height, width, 3) (8-bit RGB, no alpha)."""
    height, width, _ = image.shape
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # bit depth 8, color type 2
    flat = np.ascontiguousarray(image, dtype=np.uint8).reshape(height, width * 3)
    # Per-scanline filter byte 0 (None) prepended to every row in one go.
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), flat], axis=1).tobytes()
    idat = zlib.compress(raw, 6)
    return _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _write_atomic(path, data: bytes) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)


def write_gray8(path, width: int, rows) -> None:
    """Encode and write atomically (tmp file + os.replace) so a reader never
    sees a half-written file -- this is called repeatedly as more rows arrive."""
    _write_atomic(path, encode_gray8(width, rows))


def write_rgb8(path, image: np.ndarray) -> None:
    """RGB counterpart of write_gray8 (same atomic replace); image is (H, W, 3) uint8."""
    _write_atomic(path, encode_rgb8(image))
