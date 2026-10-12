"""Decoder readiness as three independent facts.

1. **Installation** -- is the backend here, and which version?
2. **Validation** -- has *this* backend version been shown to work, on what?
3. **Session** -- what is the decoder doing right now on the current input?

Availability is never proof of correctness: an installed decoder with an
"untested" validation record is exactly that. A validation record made
against a different backend version is *stale* and says so.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

InstallationState = Literal["present", "missing", "incompatible", "builtin"]

ValidationState = Literal[
    "untested",
    "fixture_tested",  # synthetic/built-in fixture in the test suite
    "reference_iq_tested",  # redistributable reference IQ/audio recording
    "live_output_observed",  # decoded off-air output, no independent content comparison
    "field_observed",  # off-air with an independent reference
    "regression_detected",
]

SessionState = Literal[
    "idle",
    "acquiring",  # input flowing, nothing recognised yet
    "synchronized",  # backend reports sync/lock (only where it reports one)
    "valid_frames",  # integrity-checked output seen this session
    "unverified_output",  # events emitted, integrity unavailable/unknown
    "quiet",  # input present but no signal energy
    "unsupported_input",  # e.g. wrong demodulator/rate/path for this decoder
    "error",
]


class Installation(BaseModel):
    state: InstallationState
    backend: str = ""  # "multimon", "direwolf", "dsdfme", "minimodem", "python"
    binary: str | None = None  # resolved path when present
    version: str | None = None  # None = present but version unknown
    install_hint: str = ""


class Validation(BaseModel):
    state: ValidationState = "untested"
    # What the record was made against; a different installed version makes
    # this record stale (see DecoderReadiness.validation_stale).
    backend_version: str | None = None
    validated_on: date | None = None
    # Pointers a reader can chase: test ids, fixture paths, report paths.
    tests: list[str] = Field(default_factory=list)
    fixtures: list[str] = Field(default_factory=list)
    report: str = ""
    notes: str = ""


class Session(BaseModel):
    state: SessionState = "idle"
    detail: str = ""
    events: int = Field(default=0, ge=0)
    valid_frames: int = Field(default=0, ge=0)
    # Where the input came from this session, so a replay run is never
    # mistaken for live reception.
    source_id: str | None = None


class DecoderReadiness(BaseModel):
    decoder_id: str
    installation: Installation
    validation: Validation = Field(default_factory=Validation)
    session: Session = Field(default_factory=Session)

    @property
    def validation_stale(self) -> bool:
        """The validation record predates a backend version change.

        Built-in (python) decoders are versioned with msdr itself, so their
        records go stale only when the recorded version differs too.
        """
        if self.validation.state == "untested":
            return False
        recorded = self.validation.backend_version
        current = self.installation.version
        if recorded is None or current is None:
            # Unknown either side: we can't prove freshness, so say stale.
            return True
        return recorded != current

    @property
    def usable(self) -> bool:
        """Installed well enough to *start*; says nothing about correctness."""
        return self.installation.state in ("present", "builtin")

    def as_api(self) -> dict:
        d = self.model_dump(mode="json")
        d["validation_stale"] = self.validation_stale
        d["usable"] = self.usable
        return d
