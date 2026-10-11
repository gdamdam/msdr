"""Confirmed-state history: a linear undo/redo stack of ReceiverState snapshots.

Task 8 pushes the pre-restore snapshot before an app-driven restore and the
post-restore snapshot after it confirms, so `back()` returns the previous
confirmed state and `forward()` re-applies the one that was undone.
"""

from typing import Literal

from msdr.engine.state import ReceiverState


class HistoryStack:
    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max_entries
        self.entries: list[ReceiverState] = []
        self.position: int = -1

    def push(self, state: ReceiverState) -> None:
        del self.entries[self.position + 1 :]  # drop any redo tail, even on a collapse
        if self.entries and self.entries[self.position].to_props() == state.to_props():
            return  # consecutive duplicate (same wire props) collapses into one entry
        self.entries.append(state)
        self.position = len(self.entries) - 1
        overflow = len(self.entries) - self.max_entries
        if overflow > 0:
            del self.entries[:overflow]
            self.position -= overflow

    def back(self) -> ReceiverState | None:
        if not self.can_back():
            return None
        self.position -= 1
        return self.entries[self.position]

    def forward(self) -> ReceiverState | None:
        if not self.can_forward():
            return None
        self.position += 1
        return self.entries[self.position]

    def undo_move(self, direction: Literal["back", "forward"]) -> None:
        # Reverts a back()/forward() whose restore failed, without touching entries.
        delta = 1 if direction == "back" else -1
        self.position = max(-1, min(len(self.entries) - 1, self.position + delta))

    def can_back(self) -> bool:
        return self.position > 0

    def can_forward(self) -> bool:
        return self.position < len(self.entries) - 1

    def dump(self) -> dict:
        return {"position": self.position, "state_ids": [s.id for s in self.entries]}

    @classmethod
    def load(
        cls, states: list[ReceiverState], position: int, max_entries: int = 200
    ) -> "HistoryStack":
        stack = cls(max_entries=max_entries)
        entries = list(states)
        overflow = len(entries) - max_entries
        if overflow > 0:
            entries = entries[overflow:]
            position -= overflow
        stack.entries = entries
        # A stale or corrupt persisted cursor must not make back()/forward() index out of range.
        stack.position = max(-1, min(len(entries) - 1, position))
        return stack
