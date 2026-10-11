"""Acquisition source and channel descriptors.

A *source* is where IQ (and spectrum) come from: the live SDRconnect radio or
a recording being replayed. A *channel* is a slice of a source's span that a
consumer (listener, decoder, analyzer) wants demodulated. Both carry the
``source_id`` so commands and events can be routed and so a replay can never
be mistaken for the live receiver.

Clock rules (plan §6) that every source implementation must honour:

- the *sample index* is the replay clock; wall-clock is only a pacing hint;
- ``capture_time`` is the original reception time and is ``None`` when the
  recording carries no trustworthy timestamp (WAV, legacy captures). File
  mtime and playback time are never substituted for it;
- ``generation`` increments on seek, loop wrap, rate/centre transition or
  source switch. Results tagged with an older generation are discarded;
- samples are never skipped silently to keep up with wall-clock during
  analysis -- a gap becomes a :class:`Discontinuity`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceMode = Literal["live", "replay"]
SampleFormat = Literal["ci16_le", "cf32_le"]
DiscontinuityKind = Literal[
    "seek",  # position moved by the user or a bookmark
    "loop",  # wrapped from the end back to the start
    "retune",  # centre and/or rate changed (live retune or a new SigMF run)
    "gap",  # samples missing between two runs of one recording
    "dropped",  # frames lost in transport (live stream or slow disk)
    "source_switch",  # a different source now feeds this consumer
]


class Discontinuity(BaseModel):
    """A break in the sample stream, always explicit and never inferred."""

    kind: DiscontinuityKind
    at_sample: int = Field(ge=0)
    generation: int = Field(ge=0)
    # Known gap length in samples of the *previous* run's rate; None means
    # the duration is unknown (never invented, plan §6).
    gap_samples: int | None = Field(default=None, ge=0)
    note: str = ""


class SourceDescriptor(BaseModel):
    """Identity and clock state of one acquisition source."""

    source_id: str
    mode: SourceMode
    sample_format: SampleFormat = "ci16_le"
    sample_rate_hz: int = Field(gt=0)
    center_hz: int = Field(gt=0)
    # Absolute reception time of sample 0 of the current run, if trustworthy.
    capture_time: datetime | None = None
    # Where the recording came from (path/name for replay, adapter for live).
    origin: str = ""
    # Current position in complex samples since the start of the run.
    sample_index: int = Field(default=0, ge=0)
    generation: int = Field(default=0, ge=0)
    # Total span of a replay in samples; None for a live source.
    total_samples: int | None = Field(default=None, ge=0)
    discontinuities: list[Discontinuity] = Field(default_factory=list)

    @property
    def span_hz(self) -> tuple[int, int]:
        half = self.sample_rate_hz // 2
        return (self.center_hz - half, self.center_hz + half)

    @property
    def elapsed_s(self) -> float:
        """File-relative (or run-relative) elapsed time; not reception time."""
        return self.sample_index / self.sample_rate_hz

    def bump(self, kind: DiscontinuityKind, **kw) -> "SourceDescriptor":
        """Return a copy with ``generation + 1`` and the discontinuity logged."""
        gen = self.generation + 1
        disc = Discontinuity(kind=kind, at_sample=self.sample_index, generation=gen, **kw)
        return self.model_copy(
            update={"generation": gen, "discontinuities": [*self.discontinuities, disc]}
        )


ChannelConsumer = Literal["listen", "decode", "analyze", "record"]


class ChannelDescriptor(BaseModel):
    """A demodulated slice of a source, as a consumer asked for it."""

    channel_id: str
    source_id: str
    frequency_hz: int = Field(gt=0)
    bandwidth_hz: int = Field(gt=0)
    demodulator: str  # SDRconnect demodulator name: NFM, WFM, AM, USB, LSB, CW
    consumers: list[ChannelConsumer] = Field(default_factory=list)
    # Which generation of the source this channel's DSP state belongs to;
    # a mismatch means "flush and re-initialise before trusting output".
    generation: int = Field(default=0, ge=0)

    def offset_hz(self, source: SourceDescriptor) -> int:
        return self.frequency_hz - source.center_hz

    def fits(self, source: SourceDescriptor, margin_hz: int = 0) -> bool:
        """True when the whole channel (plus filter margin) lies in the span.

        Virtual tuning in replay is bounded by the recorded span (plan §6);
        the live radio may instead retune, which is the caller's decision.
        """
        lo, hi = source.span_hz
        half = self.bandwidth_hz // 2 + margin_hz
        return lo <= self.frequency_hz - half and self.frequency_hz + half <= hi
