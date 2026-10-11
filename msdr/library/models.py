"""Library models: tags, receiver profiles and signals.

Frequencies are int Hz. Timestamps are ISO-8601 UTC strings (seconds precision).
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from msdr.engine import confirmed
from msdr.engine.state import ProfileSettings, TuneRecipe
from msdr.ids import uuid7
from msdr.library.frequency import format_mhz


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string, seconds precision, "Z" suffix."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Scanner alert set (signals) and a smaller, calmer set for category/user tags
# -- kept as separate palettes since signal colors are meant to pop against
# the waterfall while tag chips sit in list UI.
SIGNAL_COLORS = ["red", "orange", "yellow", "green", "blue", "magenta", "cyan", "white"]
TAG_COLORS = ["red", "orange", "green", "blue", "violet", "gray"]
SignalColor = Literal["red", "orange", "yellow", "green", "blue", "magenta", "cyan", "white"]
TagColor = Literal["red", "orange", "green", "blue", "violet", "gray"]

# nextpass catalog labels a signal can be pinned to for the PASSES section
# (see .superpowers/sdd/tools/contract.md); kept here alongside the other
# closed vocabularies this model already owns (colors above).
SATELLITE_LABELS = ["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"]
Satellite = Literal["M2-3", "M2-4", "ISS", "CSS", "AO-73", "RS-44", "SO-50", "AO-123"]


class Tag(BaseModel):
    id: str = Field(default_factory=uuid7)
    name: str
    kind: Literal["category", "user"] = "user"
    color: TagColor | None = None


# Short-code categories with their default colors. CATEGORY_NAMES is kept for
# anything that only needs the names (e.g. tests).
CATEGORY_TAGS: list[tuple[str, TagColor]] = [
    ("sat", "blue"),
    ("ham", "green"),
    ("mil", "red"),
    ("air", "orange"),
    ("marine", "blue"),
    ("tv", "violet"),
    ("bc", "violet"),
    ("digital", "gray"),
    ("unid", "gray"),
    ("exp", "orange"),
    ("fav", "red"),
]
CATEGORY_NAMES = [name for name, _ in CATEGORY_TAGS]


class ReceiverProfile(BaseModel):
    id: str = Field(default_factory=uuid7)
    name: str
    description: str = ""
    native_profile_name: str | None = None
    antenna: str | None = None
    lna_state: int | None = None
    sample_rate_hz: int | None = None
    squelch_enabled: bool | None = None
    squelch_threshold: float | None = None
    audio_agc_enabled: bool | None = None
    audio_agc_threshold: float | None = None
    noise_reduction_enabled: bool | None = None
    noise_reduction_strength: int | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    def settings(self) -> ProfileSettings:
        return ProfileSettings(
            native_profile_name=self.native_profile_name,
            antenna=self.antenna,
            lna_state=self.lna_state,
            sample_rate_hz=self.sample_rate_hz,
            squelch_enabled=self.squelch_enabled,
            squelch_threshold=self.squelch_threshold,
            audio_agc_enabled=self.audio_agc_enabled,
            audio_agc_threshold=self.audio_agc_threshold,
            noise_reduction_enabled=self.noise_reduction_enabled,
            noise_reduction_strength=self.noise_reduction_strength,
        )

    def matches(self, props: dict[str, object]) -> bool:
        """True if every non-null API field (native_profile_name ignored) matches
        the confirmed wire props. A profile with no API fields never matches."""
        api_fields = self.settings().to_props()
        if not api_fields:
            return False
        return all(
            confirmed.matches(prop, value, props.get(prop)) for prop, value in api_fields.items()
        )


class Signal(BaseModel):
    id: str = Field(default_factory=uuid7)
    name: str
    status: Literal["unknown", "tentative", "identified"] = "unknown"
    frequency_hz: int
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = None
    profile_id: str | None = None
    station: str = ""
    location: str = ""
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    urls: list[str] = []
    notes: str = ""
    tag_ids: list[str] = []
    # Decoder ids (e.g. "pocsag", "flex") to start automatically when this
    # memory is tuned -- see AppService.tune_signal and msdr/decoder/registry.py.
    decoders: list[str] = []
    # Optional built-in PHY preset recalled when this memory starts its decoder.
    decoder_preset: Literal["meshcore_us", "meshtastic_us_longfast"] | None = None
    first_heard_at: str | None = None
    last_heard_at: str | None = None
    heard_count: int = 0
    origin_state_id: str | None = None
    color: SignalColor | None = None
    satellite: Satellite | None = None
    # None = leave the current antenna unchanged on tune (older signals, or
    # ones never captured with an antenna reading). Staged before frequency
    # like ReceiverProfile.antenna -- see AppService.tune_signal.
    antenna: str | None = None
    # None = leave the current LNA unchanged on tune, like `antenna` above.
    lna_state: int | None = Field(default=None, ge=0, le=59)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    @model_validator(mode="after")
    def coordinates_are_paired(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self

    def recipe(self) -> TuneRecipe:
        return TuneRecipe(
            frequency_hz=self.frequency_hz,
            demodulator=self.demodulator,
            filter_bandwidth_hz=self.filter_bandwidth_hz,
        )

    @staticmethod
    def unknown_name(frequency_hz: int) -> str:
        return f"UNKNOWN {format_mhz(frequency_hz)}"
