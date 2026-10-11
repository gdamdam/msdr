"""Session / Observation / RecordingReference models (S2 contract).

Fields mirror the 012_observations.sql columns; `*_json` columns appear here
as their decoded Python value (`receiver_json` -> `receiver`). Timestamps are
epoch seconds, like `decode_events.ts` and `finds.*_ts`. `Claim` lives in
msdr.contracts.evidence and is re-exported so callers import one place.
"""

from typing import Literal

from pydantic import BaseModel, Field

from msdr.contracts.evidence import CaptureQuality, Claim, Measurement
from msdr.ids import uuid7

__all__ = ["Claim", "Observation", "RecordingReference", "Session", "SessionMode"]

SessionMode = Literal["live", "replay"]


class Session(BaseModel):
    id: str = Field(default_factory=uuid7)
    source_id: str  # "live:sdrconnect" | "replay:<recording name>"
    mode: SessionMode
    origin: str = ""  # adapter host or recording path
    started_at: float  # processing clock, epoch s
    ended_at: float | None = None
    center_hz: int | None = None
    sample_rate_hz: int | None = None
    receiver: dict | None = None
    notes: str = ""


class Observation(BaseModel):
    id: str = Field(default_factory=uuid7)
    session_id: str | None = None
    # A memory link *suggests* the same emitter; it never proves identity.
    signal_id: str | None = None
    find_id: str | None = None
    # Original capture time when known; a replayed WAV has samples, not
    # times, so these stay None there rather than using the processing clock.
    start_ts: float | None = None
    end_ts: float | None = None
    sample_start: int | None = None
    sample_end: int | None = None
    reported_hz: int | None = None
    measured_hz: int | None = None  # kept apart from reported_hz, never overwrites it
    bandwidth_hz: int | None = None
    receiver: dict | None = None
    measurements: list[Measurement] = Field(default_factory=list)
    capture_quality: list[CaptureQuality] = Field(default_factory=list)
    legacy: bool = False
    provenance: str = ""
    closed: bool = False
    # Operator free text (S5): the only field PATCH edits besides signal_id.
    notes: str = ""
    # None = let the repo stamp them on insert/update.
    created_at: float | None = None
    updated_at: float | None = None


class RecordingReference(BaseModel):
    id: str = Field(default_factory=uuid7)
    observation_id: str
    path: str
    name: str
    run_index: int | None = None
    sample_start: int | None = None
    sample_end: int | None = None
    # False = the media is gone: shown as explicitly unavailable, not hidden.
    available: bool = True
    note: str = ""
