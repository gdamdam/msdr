"""Incremental classic-pcap reader/writer.

`PcapStreamReader` is fed bytes as they arrive (pipe or file) and returns
whole records as they complete. It accepts either on-disk byte order and
both the microsecond and nanosecond magic numbers, enforces a bounded record
size (snaplen from the global header, capped by a hard ceiling) and never
raises on a truncated trailing record -- the partial bytes stay buffered
(`pending_bytes`) until more data arrives or the stream ends.

`write_header`/`write_record` are the matching encoder, used by session
export; `read_file` is a small convenience over the reader for replay/tests.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

GLOBAL_HEADER_LEN = 24
RECORD_HEADER_LEN = 16

# The four byte sequences a pcap global header's magic number can take.
# (byte_order_fmt, is_nanosecond)
_MAGICS: dict[bytes, tuple[str, bool]] = {
    b"\xa1\xb2\xc3\xd4": (">", False),
    b"\xd4\xc3\xb2\xa1": ("<", False),
    b"\xa1\xb2\x3c\x4d": (">", True),
    b"\x4d\x3c\xb2\xa1": ("<", True),
}

DEFAULT_SNAPLEN = 262144
DEFAULT_MAX_RECORD_SIZE = 65536
LINKTYPE_IEEE802_11_RADIOTAP = 127


@dataclass(frozen=True)
class Record:
    ts: float
    linktype: int
    data: bytes
    orig_len: int


class PcapStreamReader:
    """Feed bytes with `feed()`, get back whatever complete records that
    made available. Oversize records (bigger than the header's snaplen or
    `max_record_size`, whichever is smaller) are dropped and tallied in
    `oversize_dropped` rather than raising or stalling the stream."""

    def __init__(self, max_record_size: int = DEFAULT_MAX_RECORD_SIZE) -> None:
        self.max_record_size = max_record_size
        self.oversize_dropped = 0
        self._buf = bytearray()
        self._byte_order: str | None = None
        self._nanosecond = False
        self._snaplen = 0
        self._linktype = 0
        self._header_parsed = False
        # Payload bytes of an already-counted oversize record still to discard.
        self._skip = 0

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[Record]:
        self._buf += data
        out: list[Record] = []

        if not self._header_parsed:
            if len(self._buf) < GLOBAL_HEADER_LEN:
                return out
            magic = bytes(self._buf[:4])
            info = _MAGICS.get(magic)
            if info is None:
                # Not a pcap stream we understand; drop what we have so a
                # later resync attempt (if any) isn't poisoned by it.
                del self._buf[: len(self._buf)]
                return out
            self._byte_order, self._nanosecond = info
            fmt = f"{self._byte_order}HHiIII"
            (_vmaj, _vmin, _thiszone, _sigfigs, snaplen, linktype) = struct.unpack(
                fmt, self._buf[4:GLOBAL_HEADER_LEN]
            )
            self._snaplen = snaplen
            self._linktype = linktype
            del self._buf[:GLOBAL_HEADER_LEN]
            self._header_parsed = True

        divisor = 1_000_000_000.0 if self._nanosecond else 1_000_000.0
        rec_fmt = f"{self._byte_order}IIII"
        effective_cap = (
            min(self._snaplen, self.max_record_size)
            if self._snaplen > 0
            else self.max_record_size
        )

        while True:
            if self._skip:
                n = min(self._skip, len(self._buf))
                del self._buf[:n]
                self._skip -= n
                if self._skip:
                    break
            if len(self._buf) < RECORD_HEADER_LEN:
                break
            ts_sec, ts_frac, incl_len, orig_len = struct.unpack(
                rec_fmt, self._buf[:RECORD_HEADER_LEN]
            )
            if incl_len > effective_cap:
                # Oversize: drop the record and discard its payload as it
                # streams in. Waiting for the whole record would let a header
                # claiming up to 4 GB make us buffer without bound.
                del self._buf[:RECORD_HEADER_LEN]
                self._skip = incl_len
                self.oversize_dropped += 1
                continue
            if len(self._buf) < RECORD_HEADER_LEN + incl_len:
                break  # truncated final record: stays pending
            payload = bytes(self._buf[RECORD_HEADER_LEN : RECORD_HEADER_LEN + incl_len])
            del self._buf[: RECORD_HEADER_LEN + incl_len]
            ts = ts_sec + ts_frac / divisor
            out.append(Record(ts=ts, linktype=self._linktype, data=payload, orig_len=orig_len))

        return out


def write_header(
    linktype: int = LINKTYPE_IEEE802_11_RADIOTAP,
    snaplen: int = DEFAULT_SNAPLEN,
    byte_order: str = "<",
    nanosecond: bool = False,
) -> bytes:
    magic = {
        ("<", False): b"\xd4\xc3\xb2\xa1",
        (">", False): b"\xa1\xb2\xc3\xd4",
        ("<", True): b"\x4d\x3c\xb2\xa1",
        (">", True): b"\xa1\xb2\x3c\x4d",
    }[(byte_order, nanosecond)]
    return magic + struct.pack(f"{byte_order}HHiIII", 2, 4, 0, 0, snaplen, linktype)


def write_record(
    ts: float,
    data: bytes,
    orig_len: int | None = None,
    byte_order: str = "<",
    nanosecond: bool = False,
) -> bytes:
    divisor = 1_000_000_000 if nanosecond else 1_000_000
    ts_sec = int(ts)
    ts_frac = int(round((ts - ts_sec) * divisor))
    if ts_frac >= divisor:
        ts_sec += 1
        ts_frac -= divisor
    incl_len = len(data)
    header = struct.pack(
        f"{byte_order}IIII",
        ts_sec,
        ts_frac,
        incl_len,
        orig_len if orig_len is not None else incl_len,
    )
    return header + data


def read_file(path: str | Path) -> list[Record]:
    reader = PcapStreamReader()
    return reader.feed(Path(path).read_bytes())
