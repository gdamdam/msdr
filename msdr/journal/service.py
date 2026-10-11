"""Journal service: records receiver actions and "heard again" observations,
persisting the state involved and linking observations back to known signals.
"""

from typing import Literal

from msdr.engine.state import Observation, ReceiverState
from msdr.library.models import Signal, now_iso
from msdr.library.search import match_heard
from msdr.storage.repos import JournalEntry, JournalRepo, SignalRepo, StateRepo


class JournalService:
    def __init__(
        self, journal: JournalRepo, signals: SignalRepo, states: StateRepo, *, nearby_hz: int
    ) -> None:
        self.journal = journal
        self.signals = signals
        self.states = states
        self.nearby_hz = nearby_hz

    def record_action(
        self,
        kind: Literal["capture", "tune", "back", "forward", "profile", "identified", "antenna"],
        *,
        state: ReceiverState | None,
        signal_id: str | None = None,
        profile_id: str | None = None,
        observation: Observation | None = None,
        note: str | None = None,
    ) -> JournalEntry:
        if state is not None:
            self.states.add(state)  # OR IGNORE: safe to re-add a state already on record
        entry = JournalEntry(
            kind=kind,
            state_id=state.id if state is not None else None,
            signal_id=signal_id,
            profile_id=profile_id,
            signal_power_db=observation.signal_power_db if observation is not None else None,
            signal_snr_db=observation.signal_snr_db if observation is not None else None,
            note=note,
        )
        self.journal.add(entry)
        return entry

    def record_observed(self, state: ReceiverState, observation: Observation) -> JournalEntry:
        self.states.add(state)
        # near() filters in SQL, so a candidate can't be lost to the page
        # limit signals.list() applies before any distance check happens.
        radius = int(max((state.filter_bandwidth_hz or 0) / 2, self.nearby_hz))
        candidates = self.signals.near(state.vfo_hz, radius)
        match = match_heard(candidates, state, nearby_hz=self.nearby_hz)
        signal_id = None
        if match is not None:
            self.touch_heard(match)
            signal_id = match.id
        entry = JournalEntry(
            kind="observed",
            state_id=state.id,
            signal_id=signal_id,
            signal_power_db=observation.signal_power_db,
            signal_snr_db=observation.signal_snr_db,
        )
        self.journal.add(entry)
        return entry

    def touch_heard(self, signal: Signal, at: str | None = None) -> Signal:
        at = at or now_iso()
        updated = signal.model_copy(
            update={
                "heard_count": signal.heard_count + 1,
                "last_heard_at": at,
                "first_heard_at": signal.first_heard_at or at,
            }
        )
        self.signals.update(updated)  # update() stamps updated_at itself
        return updated
