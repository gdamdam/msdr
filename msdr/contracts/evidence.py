"""Inspectable claims instead of a confidence number (plan §3).

Evidence types are *independent sources*. They are never summed into one
ladder and no heuristic output is ever presented as a probability. What each
type supports -- and what it does not establish -- is fixed here so that the
UI, storage and analyzers agree:

=====================  ==================================  ==========================
type                   supports                            does not establish
=====================  ==================================  ==========================
allocation             frequency lies in an allocated      a transmission is present
                       service/channel                     or belongs to that service
schedule               a catalog predicts a transmitter    reception or identity
                       at this time/frequency
iq_estimate            measured properties support         exact protocol or station
                       modulation/protocol candidates
decoder_output         a decoder emitted an event          correctness (unless integrity
                                                           checks support it)
validated_decode       protocol-specific checks passed     identity beyond the decoded
                                                           fields themselves
operator               a person supplied an identification independent machine
                       and rationale                       verification
=====================  ==================================  ==========================

Heuristic scores may exist for ranking and diagnostics; they are called
``score`` and stay unitless. Measured percentages (duty cycle, frame success)
are :class:`Measurement` values with a denominator and window.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from msdr.ids import uuid7

EvidenceType = Literal[
    "allocation",
    "schedule",
    "iq_estimate",
    "decoder_output",
    "validated_decode",
    "operator",
]

# What a claim is about. "presence" is on/off activity (a keyed carrier is a
# presence claim, not a CW claim); "service" is what an allocation names.
ClaimSubject = Literal["presence", "modulation", "protocol", "service", "station"]

IntegrityStatus = Literal[
    "not_applicable",  # the claim type has no integrity check (allocation, operator)
    "unavailable",  # the backend does not expose one -- never invented as success
    "unknown",  # a check exists but has not run on this output yet
    "passed",
    "failed",
]

# Capture quality is displayed separately from claims (plan §3): quiet is
# not data and not-voice is not a protocol identification.
CaptureQuality = Literal[
    "sufficient",
    "insufficient_data",
    "signal_present",
    "quiet",
    "clipping",
    "missing_frames",
    "off_channel",
    "unsupported_path",
    "ambiguous",
    "conflicting",
]

# The one qualitative state per subject, each with an observable rule (see
# evidence_state()). There is deliberately no "confirmed" reachable from
# allocation or schedule evidence.
EvidenceState = Literal[
    "none",  # no claims at all
    "context",  # only allocation and/or schedule claims
    "estimated",  # an IQ estimate or unvalidated decoder output exists
    "validated",  # a validated_decode claim with integrity == passed
    "operator",  # a person identified it (shown as such, not as machine-verified)
]

MeasurementUnit = Literal["dBFS", "dBm", "dB", "Hz", "baud", "s", "%", "count", "ratio"]


class Measurement(BaseModel):
    """A number with its method and reference, so it can be labelled correctly.

    dBFS, dBm and dB (SNR) are different quantities and stay different units;
    an unknown calibration is not converted. A percentage must say what it
    divides by and over which window (plan §3).
    """

    name: str
    value: float
    unit: MeasurementUnit
    method: str = ""  # e.g. "spectrum_display_snr", "iq_obw99", "receiver_signal_power"
    reference: str = ""  # e.g. "SDRconnect signal_power", "noise floor median"
    window_s: float | None = Field(default=None, ge=0)
    denominator: str = ""  # required for "%": what 100 % means
    measured_at: datetime | None = None

    @model_validator(mode="after")
    def _percent_needs_denominator(self) -> "Measurement":
        if self.unit == "%" and (not self.denominator or self.window_s is None):
            raise ValueError("a percentage needs a denominator and a measurement window")
        return self


class Claim(BaseModel):
    """One inspectable assertion about a subject, from one evidence source."""

    claim_id: str = Field(default_factory=uuid7)
    subject: ClaimSubject
    value: str  # e.g. "NFM", "AX.25 AFSK 1200", "KNBR", "Marine VHF"
    evidence_type: EvidenceType
    observation_id: str | None = None
    source_id: str | None = None
    # Set when this claim comes from a review/replay decode session (S4),
    # never a live one -- so a UI or a later query can tell "this decode
    # happened during analysis of a recording" from "this happened live",
    # and live encounter counts are never inflated by re-analysis.
    analysis_run_id: str | None = None
    # Observed interval: absolute times when known, otherwise the sample
    # range on the source (replay of a WAV has samples but no capture time).
    observed_start: datetime | None = None
    observed_end: datetime | None = None
    sample_start: int | None = Field(default=None, ge=0)
    sample_end: int | None = Field(default=None, ge=0)
    producer: str  # "survey.analyzer", "decoder.aprs", "catalog.eibi", "operator"
    producer_version: str = ""
    support: list[str] = Field(default_factory=list)  # measurements/events it rests on
    measurements: list[Measurement] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    integrity: IntegrityStatus = "unknown"
    # Internal heuristic ranking score. Unitless, uncalibrated, never shown as
    # a probability or percentage.
    score: float | None = None
    next_action: str = ""
    created_at: datetime | None = None
    # A newer analysis or an operator correction points here instead of
    # overwriting (append-only, plan §7). Historical claims stay inspectable.
    superseded_by: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Claim":
        if self.evidence_type in ("allocation", "schedule", "operator"):
            if self.integrity not in ("not_applicable", "unavailable"):
                raise ValueError(f"{self.evidence_type} evidence has no integrity check")
        if self.evidence_type == "validated_decode" and self.integrity != "passed":
            raise ValueError("validated_decode requires integrity == passed")
        if (self.sample_start is None) != (self.sample_end is None):
            raise ValueError("sample_start and sample_end go together")
        if self.sample_start is not None and self.sample_end < self.sample_start:
            raise ValueError("sample_end before sample_start")
        return self

    @property
    def historical(self) -> bool:
        return self.superseded_by is not None


# Observable rules, in order of precedence. Allocation/schedule alone can
# only ever produce "context".
_STATE_RANK: dict[EvidenceState, int] = {
    "none": 0,
    "context": 1,
    "estimated": 2,
    "validated": 3,
    "operator": 3,
}


def claim_state(claim: Claim) -> EvidenceState:
    # Historical claims from the removed experimental RF ML feature stay in
    # existing databases; they remain inspectable but never count as evidence.
    if claim.producer in ("survey.rfml", "survey.rfml.detector"):
        return "none"
    if claim.evidence_type in ("allocation", "schedule"):
        return "context"
    if claim.evidence_type == "operator":
        return "operator"
    if claim.evidence_type == "validated_decode":
        return "validated"
    return "estimated"


def evidence_state(claims: list[Claim], *, include_historical: bool = False) -> EvidenceState:
    """The strongest state the *current* claims support.

    Superseded claims are ignored unless asked for. "validated" and
    "operator" are peers (a person's identification is displayed as such and
    never upgraded to machine-verified).
    """
    best: EvidenceState = "none"
    for c in claims:
        if c.historical and not include_historical:
            continue
        s = claim_state(c)
        if _STATE_RANK[s] > _STATE_RANK[best]:
            best = s
    return best


def identity_established(claims: list[Claim]) -> bool:
    """True only when a station/protocol claim is validated or operator-supplied.

    Frequency allocation and schedule are context, never identity (plan §3,
    §5 AIS case). An IQ estimate names candidates, not an emitter.
    """
    for c in claims:
        if c.historical or c.subject not in ("station", "protocol"):
            continue
        if claim_state(c) in ("validated", "operator"):
            return True
    return False
