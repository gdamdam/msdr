"""Confirmed receiver state. Extended by the engine task."""
import asyncio
from dataclasses import dataclass
from typing import Literal

from msdr.engine.state import Observation, ReceiverState
from msdr.radio.protocol import PropertyChanged
from msdr.radio.registry import REGISTRY, RESTORABLE

_MISSING = object()


def matches(prop: str, a: object, b: object) -> bool:
    """Numbers compare within the registry tolerance; everything else by equality."""
    spec = REGISTRY.get(prop)
    if (
        isinstance(a, int | float)
        and isinstance(b, int | float)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        tol = spec.tolerance if spec else 0.0
        return abs(float(a) - float(b)) <= tol
    return a == b


@dataclass(frozen=True)
class StateChanged:
    property: str
    old: object
    new: object
    cause: Literal["own", "external"]
    at: float


class ConfirmedState:
    def __init__(self, api_version: str = "") -> None:
        self.props: dict[str, object] = {}
        self.api_version = api_version
        self._expectations: dict[str, object] = {}
        self._subscribers: list[asyncio.Queue[StateChanged]] = []
        # Restorable props carried over from a connection that has since
        # dropped: still in `props`, but no longer trustworthy.
        self._stale: set[str] = set()

    def mark_stale(self) -> None:
        """The connection dropped: every restorable value is now only a guess.

        Values stay in `props` so the UI keeps showing the last thing known,
        but is_complete() reports False -- and capture/restore refuse -- until
        each one has been re-read from the new connection. Without this, a
        reconnect read that times out silently keeps the previous
        connection's value (the radio says antenna C, msdr still says A).
        """
        self._stale = {spec.name for spec in RESTORABLE}

    def seed(self, props: dict[str, object]) -> None:
        self.props.update(props)
        self._stale -= props.keys()

    def expect(self, prop: str, value: object) -> None:
        self._expectations[prop] = value

    def clear_expectations(self) -> None:
        self._expectations.clear()

    def apply(self, ev: PropertyChanged) -> "StateChanged | None":
        # The receiver state tracked by msdr is the primary SDRconnect SP.
        # RSPduo can report a second tuner over the same WebSocket; its
        # properties have the same names and must not overwrite the primary
        # state or interrupt a restore that is controlling the primary.
        if ev.device != "primary":
            return None

        prop = ev.property
        spec = REGISTRY.get(prop)
        old = self.props.get(prop)
        self._stale.discard(prop)  # a live event is a fresh read for this prop
        if spec is not None and spec.read_only and prop != "started":
            # Read-only props (signal_power etc.) update silently -- they are
            # never restore targets, so they never produce a StateChanged.
            # `started` is also read-only, but its transitions drive receiver
            # lifecycle consumers such as the UI and decoder manager.
            self.props[prop] = ev.value
            return None
        expected = self._expectations.get(prop, _MISSING)
        if expected is not _MISSING and matches(prop, expected, ev.value):
            # An echo of a pending own set, even a no-op one (SDRconnect
            # echoes sets that don't change anything).
            self._expectations.pop(prop, None)
            self.props[prop] = ev.value
            sc = StateChanged(prop, old, ev.value, "own", ev.at)
            self._emit(sc)
            return sc
        if old is not None and matches(prop, old, ev.value):
            self.props[prop] = ev.value
            return None
        self.props[prop] = ev.value
        sc = StateChanged(prop, old, ev.value, "external", ev.at)
        self._emit(sc)
        return sc

    def snapshot(self, source: str) -> ReceiverState:
        if not self.is_complete():
            raise ValueError("confirmed state is incomplete")
        return ReceiverState.from_props(self.props, source=source, api_version=self.api_version)

    def observation(self) -> Observation:
        return Observation(
            signal_power_db=self.props.get("signal_power"),
            signal_snr_db=self.props.get("signal_snr"),
            overload=self.props.get("overload"),
        )

    def is_complete(self) -> bool:
        if self._stale:
            return False
        return all(spec.name in self.props for spec in RESTORABLE)

    def subscribe(self) -> asyncio.Queue[StateChanged]:
        q: asyncio.Queue[StateChanged] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[StateChanged]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _emit(self, sc: StateChanged) -> None:
        for q in self._subscribers:
            q.put_nowait(sc)
