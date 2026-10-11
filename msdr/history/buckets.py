"""`MinuteAccumulator`: the in-memory side of device history (docs/history.md).

Observation tables call `add()` for every trusted observation; the service
drains it into `HistoryStore` every 30 s. Draining hands over everything and
starts empty, and the store merges additively, so a minute split across two
flushes still sums correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def minute_of(ts: float) -> int:
    return int(ts // 60) * 60


@dataclass
class Bucket:
    reports: int = 0
    sig_min: float | None = None
    sig_max: float | None = None
    sig_sum: float = 0.0
    sig_n: int = 0

    def add(self, signal: float | None) -> None:
        self.reports += 1
        if signal is None:
            return
        self.sig_min = signal if self.sig_min is None else min(self.sig_min, signal)
        self.sig_max = signal if self.sig_max is None else max(self.sig_max, signal)
        self.sig_sum += signal
        self.sig_n += 1


@dataclass
class DeviceMeta:
    name: str | None = None
    kind: str | None = None
    signal_kind: str | None = None
    first_seen: float | None = None
    last_seen: float | None = None


@dataclass
class Drained:
    buckets: dict[tuple[str, str, int], Bucket] = field(default_factory=dict)
    devices: dict[tuple[str, str], DeviceMeta] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.buckets or self.devices)


# Cap on pending (receiver, ident, minute) keys between flushes. The service
# drains every 30 s, so this is only reached by a flood of distinct
# identifiers (>1600 new devices/s, e.g. random BLE addresses under attack
# or a mis-parsing helper). New keys beyond the cap are dropped and counted
# (`dropped`, cumulative, in GET /api/history/status); buckets already
# pending keep updating, so memory stays flat instead of growing until the
# next flush.
DEFAULT_MAX_PENDING = 50_000


class MinuteAccumulator:
    def __init__(self, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self._pending = Drained()
        self.max_pending = max_pending
        self.dropped = 0

    def add(
        self,
        receiver: str,
        ident: str,
        ts: float,
        signal: float | None,
        *,
        name: str | None = None,
        kind: str | None = None,
        signal_kind: str | None = None,
    ) -> None:
        key = (receiver, ident, minute_of(ts))
        bucket = self._pending.buckets.get(key)
        if bucket is None:
            if len(self._pending.buckets) >= self.max_pending:
                self.dropped += 1
                return
            bucket = self._pending.buckets[key] = Bucket()
        bucket.add(signal)
        meta = self._pending.devices.get((receiver, ident))
        if meta is None:
            meta = self._pending.devices[(receiver, ident)] = DeviceMeta(first_seen=ts)
        meta.first_seen = ts if meta.first_seen is None else min(meta.first_seen, ts)
        meta.last_seen = ts if meta.last_seen is None else max(meta.last_seen, ts)
        # Latest non-empty values win: a hidden SSID or nameless report must
        # not erase a name heard earlier.
        if name:
            meta.name = name
        if kind:
            meta.kind = kind
        if signal_kind and signal is not None:
            meta.signal_kind = signal_kind

    def drain(self) -> Drained:
        out, self._pending = self._pending, Drained()
        return out

    def __len__(self) -> int:
        return len(self._pending.buckets)
