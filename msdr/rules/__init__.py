"""Watch rules (docs/rules.md, TODO "Phase 6 -- Watch rules"): beacon
presence/absence, BLE sensor thresholds and Remote ID drone geofences,
evaluated on a tick by `msdr.api.services.rules.RulesServiceMixin` and fed by
the same live tables, labels and tracker the rest of msdr already exposes.

This package has no I/O of its own:

- `msdr.rules.geo`: plain-geometry helpers (haversine distance, point in
  polygon) used by the drone geofence rule kind.
- `msdr.rules.engine`: pure evaluation -- `Rule`/`Context`/`EngineState`
  dataclasses and `evaluate()`. No asyncio, no database, no FastAPI; this is
  what `tests/rules/` exercises directly on fixtures.
- `msdr.rules.store`: `RulesRepo`, the SQLite side in the main database
  (migration 022), parallel to `msdr.history.labels.LabelsRepo`.
"""
