"""LIBRARY view: signals, lists, saved searches, duplicates/merge.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import typing

from fastapi import HTTPException

from msdr.api.schemas import SignalStatus
from msdr.api.services._common import _ALL_SIGNALS_LIMIT
from msdr.library.models import SIGNAL_COLORS, Signal


def _bulk_undo_label(
    op: str,
    value: str | None,
    affected: int,
    *,
    tag_name: str | None,
    list_name: str | None = None,
) -> str:
    """Human label for a bulk op's undo entry, e.g. "delete 12 memories",
    "tag +DX on 5 memories", "status identified on 3 memories"."""
    if op == "delete":
        return f"delete {affected} memories"
    if op == "tag_add":
        return f"tag +{tag_name} on {affected} memories"
    if op == "tag_remove":
        return f"tag -{tag_name} on {affected} memories"
    if op == "set_status":
        return f"status {value} on {affected} memories"
    if op == "list_add":
        return f"add {affected} memories to {list_name}"
    if op == "list_remove":
        return f"remove {affected} memories from {list_name}"
    assert op == "set_color"
    return f"color {value or 'none'} on {affected} memories"


class LibraryServiceMixin:
    # --- library (LIBRARY view: create/edit/delete, bulk ops, undo) --------

    def create_signal(self, signal: Signal) -> Signal:
        self.library_undo.record(f"create {signal.name}", [(signal.id, None)])
        self.signals.add(signal)
        return signal

    def update_signal(self, existing: Signal, updated: Signal) -> Signal:
        self.library_undo.record(f"edit {updated.name}", [(existing.id, existing)])
        self.signals.update(updated)
        return updated

    def delete_signal(self, signal: Signal) -> None:
        self.library_undo.record(f"delete {signal.name}", [(signal.id, signal)])
        self.signals.delete(signal.id)

    def bulk_library_op(self, ids: list[str], op: str, value: str | None) -> dict:
        """POST /api/library/bulk. Unknown ids are silently skipped (not
        counted in `affected`); validation (tag/list exists, status/color are
        valid) happens before anything is mutated."""
        tag = None
        lst = None
        if op in ("tag_add", "tag_remove"):
            tag = self.tags.get(value) if value else None
            if tag is None:
                raise HTTPException(404, f"tag not found: {value!r}")
        elif op == "set_status":
            if value not in typing.get_args(SignalStatus):
                raise HTTPException(422, f"invalid status: {value!r}")
        elif op == "set_color":
            if value is not None and value not in SIGNAL_COLORS:
                raise HTTPException(422, f"invalid color: {value!r}")
        elif op in ("list_add", "list_remove"):
            lst = self.lists.get(value) if value else None
            if lst is None:
                raise HTTPException(404, f"list not found: {value!r}")
        elif op != "delete":
            raise HTTPException(422, f"unknown op: {op!r}")

        signals = [s for s in (self.signals.get(i) for i in ids) if s is not None]
        before = [(s.id, s) for s in signals]
        affected = len(signals)
        label = _bulk_undo_label(
            op,
            value,
            affected,
            tag_name=tag.name if tag else None,
            list_name=lst["name"] if lst else None,
        )
        # Recorded before mutating: delete (and list_remove) below cascade-
        # clear list_items, so the before-image's captured memberships must
        # see them first -- recording after, like this used to, would already
        # find them gone.
        self.library_undo.record(label, before)

        if op == "delete":
            for signal in signals:
                self.signals.delete(signal.id)
        elif op == "tag_add":
            for signal in signals:
                if tag.id not in signal.tag_ids:
                    self.signals.set_tags(signal.id, [*signal.tag_ids, tag.id])
        elif op == "tag_remove":
            for signal in signals:
                if tag.id in signal.tag_ids:
                    self.signals.set_tags(signal.id, [t for t in signal.tag_ids if t != tag.id])
        elif op == "set_status":
            for signal in signals:
                self.signals.update(signal.model_copy(update={"status": value}))
        elif op == "set_color":
            for signal in signals:
                self.signals.update(signal.model_copy(update={"color": value}))
        elif op == "list_add":
            self.lists.add_items(value, [s.id for s in signals])
        else:
            assert op == "list_remove"
            self.lists.remove_items(value, [s.id for s in signals])

        return {"affected": affected, "undo": self.library_undo.latest()}

    # --- lists (LIBRARY groupings: CRUD, memberships, saved searches) ------

    def create_list(self, name: str) -> dict:
        if self.lists.get_by_name(name) is not None:
            raise HTTPException(409, "list name already in use")
        return self.lists.create(name)

    def update_list(
        self, list_id: str, *, name: str | None = None, position: int | None = None
    ) -> dict:
        if self.lists.get(list_id) is None:
            raise HTTPException(404, "list not found")
        if name is not None:
            conflict = self.lists.get_by_name(name)
            if conflict is not None and conflict["id"] != list_id:
                raise HTTPException(409, "list name already in use")
        return self.lists.update(list_id, name=name, position=position)

    def delete_list(self, list_id: str) -> None:
        lst = self.lists.get(list_id)
        if lst is None:
            raise HTTPException(404, "list not found")
        if lst["builtin"]:
            raise HTTPException(409, "cannot delete a builtin list")
        self.lists.delete(list_id)

    def list_items(self, list_id: str) -> list[Signal]:
        if self.lists.get(list_id) is None:
            raise HTTPException(404, "list not found")
        return self.signals.get_many(self.lists.item_ids(list_id))

    def add_signals_to_list(self, list_id: str, ids: list[str]) -> dict:
        lst = self.lists.get(list_id)
        if lst is None:
            raise HTTPException(404, "list not found")
        existing = set(self.lists.item_ids(list_id))
        new_ids = [
            i for i in dict.fromkeys(ids) if i not in existing and self.signals.get(i) is not None
        ]
        if new_ids:
            before = [(i, self.signals.get(i)) for i in new_ids]
            self.library_undo.record(f"add {len(new_ids)} to {lst['name']}", before)
            self.lists.add_items(list_id, new_ids)
        return {"added": len(new_ids)}

    def remove_signals_from_list(self, list_id: str, ids: list[str]) -> dict:
        lst = self.lists.get(list_id)
        if lst is None:
            raise HTTPException(404, "list not found")
        existing = set(self.lists.item_ids(list_id))
        to_remove = [i for i in dict.fromkeys(ids) if i in existing]
        if to_remove:
            before = [(i, self.signals.get(i)) for i in to_remove]
            self.library_undo.record(f"remove {len(to_remove)} from {lst['name']}", before)
            self.lists.remove_items(list_id, to_remove)
        return {"removed": len(to_remove)}

    def set_list_order(self, list_id: str, ids: list[str]) -> None:
        if self.lists.get(list_id) is None:
            raise HTTPException(404, "list not found")
        current = self.lists.item_ids(list_id)
        if len(ids) != len(current) or set(ids) != set(current):
            raise HTTPException(422, "ids must be a permutation of the list's current items")
        self.lists.set_order(list_id, ids)

    def set_list_item_skip(self, list_id: str, signal_id: str, skip: bool) -> dict:
        if self.lists.get(list_id) is None:
            raise HTTPException(404, "list not found")
        if not self.lists.set_item_skip(list_id, signal_id, skip):
            raise HTTPException(404, "signal not in list")
        return {"skip": skip}

    _SAVED_SEARCH_PARAM_KEYS = frozenset(
        {"q", "status", "tag", "list", "lo_hz", "hi_hz", "sort", "order"}
    )

    def create_saved_search(self, name: str, params: dict) -> dict:
        unknown = set(params) - self._SAVED_SEARCH_PARAM_KEYS
        if unknown:
            raise HTTPException(422, f"unknown saved-search params: {sorted(unknown)}")
        return self.saved_searches.create(name, params)

    def find_duplicates(self, tolerance_hz: int) -> list[list[Signal]]:
        """Signals with the same demodulator (None matches None) whose
        frequencies chain within `tolerance_hz` of their sorted neighbour --
        transitive, so a run of near frequencies groups even if the ends are
        more than `tolerance_hz` apart. Only groups of 2+ are returned."""
        by_demodulator: dict[str | None, list[Signal]] = {}
        for signal in self.signals.list(limit=_ALL_SIGNALS_LIMIT):
            by_demodulator.setdefault(signal.demodulator, []).append(signal)

        groups: list[list[Signal]] = []
        for signals in by_demodulator.values():
            signals.sort(key=lambda s: s.frequency_hz)
            current = signals[:1]
            for prev, cur in zip(signals, signals[1:], strict=False):
                if cur.frequency_hz - prev.frequency_hz <= tolerance_hz:
                    current.append(cur)
                else:
                    if len(current) >= 2:
                        groups.append(current)
                    current = [cur]
            if len(current) >= 2:
                groups.append(current)
        return groups

    def merge_signals(self, keep_id: str, merge_ids: list[str]) -> Signal:
        keep = self.signals.get(keep_id)
        if keep is None:
            raise HTTPException(404, f"signal not found: {keep_id!r}")
        ids = [i for i in dict.fromkeys(merge_ids) if i != keep_id]
        merged = [s for s in (self.signals.get(i) for i in ids) if s is not None]
        if not merged:
            raise HTTPException(422, "merge_ids must name at least one other signal")

        # Captured before anything is mutated, both for the undo before-image
        # (which needs merged signals' rows/tags/lists to re-create them) and
        # for the journal entries about to be repointed onto keep.id.
        journal_repoint = [
            (entry_id, m.id)
            for m in merged
            for entry_id in self.journal_repo.signal_entry_ids(m.id)
        ]
        before = [(keep.id, keep)] + [(m.id, m) for m in merged]
        label = f"merge {len(merged) + 1} memories into {keep.name}"
        self.library_undo.record(label, before, journal_repoint=journal_repoint)

        tag_ids = list(dict.fromkeys(keep.tag_ids + [t for m in merged for t in m.tag_ids]))
        urls = list(dict.fromkeys(keep.urls + [u for m in merged for u in m.urls]))
        heard_count = keep.heard_count + sum(m.heard_count for m in merged)
        firsts = [t for t in [keep.first_heard_at, *(m.first_heard_at for m in merged)] if t]
        lasts = [t for t in [keep.last_heard_at, *(m.last_heard_at for m in merged)] if t]
        notes = list(dict.fromkeys(n for n in [keep.notes, *(m.notes for m in merged)] if n))

        updated = keep.model_copy(
            update={
                "tag_ids": tag_ids,
                "urls": urls,
                "heard_count": heard_count,
                "first_heard_at": min(firsts) if firsts else None,
                "last_heard_at": max(lasts) if lasts else None,
                "notes": "\n".join(notes),
                "station": keep.station or next((m.station for m in merged if m.station), ""),
                "location": keep.location or next((m.location for m in merged if m.location), ""),
            }
        )

        # List memberships: union, keep's own position wins where it's
        # already a member of a list a merged signal also belongs to.
        positions: dict[str, int] = dict(self.lists.memberships_for(keep.id))
        for m in merged:
            for list_id, position in self.lists.memberships_for(m.id):
                positions.setdefault(list_id, position)

        self.signals.update(updated)
        self.lists.set_memberships(updated.id, positions)
        for m in merged:
            self.journal_repo.repoint_signal(m.id, updated.id)
            self.signals.delete(m.id)
        return updated
