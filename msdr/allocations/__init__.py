"""Frequency allocation reference layer (see docs/allocations.md)."""

from __future__ import annotations

from typing import Any

from .model import Band, Footnote, Service, SourceStatus

__all__ = ["AllocationStore", "Band", "Footnote", "Service", "SourceStatus"]


def __getattr__(name: str) -> Any:
    # Lazy so `python -m msdr.allocations.importer` does not import the store
    # (and the importer through it) before runpy executes the module.
    if name == "AllocationStore":
        from .store import AllocationStore

        return AllocationStore
    raise AttributeError(name)
