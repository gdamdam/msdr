"""Shared contracts for the listening workbench (epic S0).

Three small, dependency-free vocabularies that every later stage builds on:

- :mod:`msdr.contracts.source` -- what an acquisition source (live radio or
  a replayed recording) and a channel on it look like, including the clock
  and generation rules replay must follow.
- :mod:`msdr.contracts.evidence` -- inspectable claims with independent
  evidence types, capture-quality flags and the rule that keeps a frequency
  allocation or a schedule from ever becoming "confirmed" identity.
- :mod:`msdr.contracts.readiness` -- three separate decoder facts:
  installed, validated, and what this session is currently doing.

Pydantic models (like ``msdr.library.models``) so the same objects serialise
to the API and to storage without a second schema.
"""
