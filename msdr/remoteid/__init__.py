"""Offline ASTM F3411 / ASD-STAN Open Drone ID (Remote ID) decoding
(docs/remote-id.md). Pure Python: message codec (`messages.py`), extraction
from already-parsed Wi-Fi/BLE frames (`transport.py`), and per-UAS
aggregation (`tracker.py`). No network or storage I/O in this package.

Message encode/decode semantics are derived from
`opendroneid/opendroneid-core-c` (Apache-2.0) at commit
`6484f26545d4f012682524e2d843fab0fbdc0b34`; see each module's docstring and
`THIRD_PARTY_NOTICES.md`.
"""

from __future__ import annotations
