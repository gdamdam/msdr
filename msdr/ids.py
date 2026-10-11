"""RFC 9562 UUIDv7 generation, no external dependency.

Layout: 48-bit ms timestamp | 4-bit version | 12-bit rand_a | 2-bit variant | 62-bit rand_b.
"""

import os
import threading
import time

_lock = threading.Lock()
# Counter lives in the rand_a bits so ids minted within the same millisecond
# still compare in creation order (pure randomness there would not sort).
_state = {"ts_ms": -1, "counter": 0}


def uuid7() -> str:
    with _lock:
        ts_ms = time.time_ns() // 1_000_000
        if ts_ms <= _state["ts_ms"]:
            ts_ms = _state["ts_ms"]
            _state["counter"] += 1
            if _state["counter"] > 0xFFF:
                # rand_a exhausted for this millisecond; advance the clock
                # value by hand to preserve strict ordering.
                ts_ms += 1
                _state["counter"] = 0
        else:
            _state["counter"] = 0
        _state["ts_ms"] = ts_ms
        rand_a = _state["counter"] & 0xFFF

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    buf = bytearray(16)
    buf[0:6] = ts_ms.to_bytes(6, "big")
    buf[6] = 0x70 | (rand_a >> 8)
    buf[7] = rand_a & 0xFF
    buf[8] = 0x80 | (rand_b >> 56)
    buf[9:16] = (rand_b & ((1 << 56) - 1)).to_bytes(7, "big")

    h = buf.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
