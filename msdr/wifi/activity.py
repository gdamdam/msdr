"""`ActivityAccumulator`: in-memory accumulation for the Wi-Fi activity
heatmap (docs/wifi.md "Wi-Fi activity"), keyed by (second, channel).

Owned by `WifiSupervisor` the same way `self.history` (a `MinuteAccumulator`)
is: `_aggregate` adds one packet per ingested pcap record, attributed to the
channel the receiver was actually tuned to at receipt (`self.channel`), never
a beacon's DS Parameter Set channel. `_tick` credits listened wall-clock time
to the current second/channel once a second while actually receiving --
drained by the same 30 s history flush that drains `MinuteAccumulator`
(`HistoryServiceMixin._history_flush`), then written to the `wifi_activity`
table additively so a flush boundary never loses or double-counts a second.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: A tick's credited wall time is capped here so a stalled event loop (a long
#: GC pause, a late-firing tick) can never invent listened coverage beyond
#: what a healthy ~1 Hz tick would produce.
TICK_CREDIT_CAP_S = 2.0


@dataclass
class ActivityCell:
    packets: int = 0
    listened_s: float = 0.0
    sig_sum: float = 0.0
    sig_n: int = 0

    def add_packet(self, signal: float | None) -> None:
        self.packets += 1
        if signal is not None:
            self.sig_sum += signal
            self.sig_n += 1

    def add_listened(self, seconds: float) -> None:
        self.listened_s += seconds


@dataclass
class DrainedActivity:
    cells: dict[tuple[int, int], ActivityCell] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.cells)


class ActivityAccumulator:
    """Keyed by (int(second), channel). Never interpolates: a (second,
    channel) pair this accumulator was never told about simply has no row,
    and the heatmap draws it as "not listened" (hatched) rather than zero."""

    def __init__(self) -> None:
        self._pending = DrainedActivity()

    def _cell(self, second: int, channel: int) -> ActivityCell:
        key = (second, channel)
        cell = self._pending.cells.get(key)
        if cell is None:
            cell = self._pending.cells[key] = ActivityCell()
        return cell

    def add_packet(self, ts: float, channel: int, signal: float | None) -> None:
        """One received pcap record, attributed to `channel` -- the caller
        passes `supervisor.channel` (the tuned channel at receipt), never a
        frame's own DS channel."""
        self._cell(int(ts), channel).add_packet(signal)

    def credit_listened(self, now: float, channel: int, elapsed: float) -> None:
        """`elapsed` seconds of listening on `channel`, credited to the
        second containing `now`. Callers are expected to have already capped
        `elapsed` (see `TICK_CREDIT_CAP_S`) and to skip the call entirely
        while not actually receiving."""
        if elapsed <= 0:
            return
        self._cell(int(now), channel).add_listened(elapsed)

    def drain(self) -> DrainedActivity:
        out, self._pending = self._pending, DrainedActivity()
        return out

    def __len__(self) -> int:
        return len(self._pending.cells)
