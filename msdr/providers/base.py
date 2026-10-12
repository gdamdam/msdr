"""Shared types for identify providers."""

from typing import Protocol

from pydantic import BaseModel


class IdentifyQuery(BaseModel):
    frequency_hz: int
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = None
    notes: str = ""
    region: str = "US"


class Candidate(BaseModel):
    name: str
    category: str | None
    confidence: float
    reason: str
    provider: str
    # Channel centre if a channel matched; None for a band-only match.
    frequency_hz: int | None = None
    demodulator: str | None = None


class IdentifyProvider(Protocol):
    name: str

    def identify(self, q: IdentifyQuery) -> list[Candidate]: ...
