"""Receiver state models. Extended by the engine task; keep field names stable."""
import time
from typing import Literal

from pydantic import BaseModel, Field

from msdr.ids import uuid7
from msdr.radio.registry import FIELD_TO_PROP, RESTORABLE


class ReceiverState(BaseModel, frozen=True):
    id: str = Field(default_factory=uuid7)
    vfo_hz: int
    center_hz: int
    sample_rate_hz: int
    demodulator: str
    filter_bandwidth_hz: int
    lna_state: int
    squelch_enabled: bool
    squelch_threshold: float
    audio_agc_enabled: bool
    audio_agc_threshold: float
    noise_reduction_enabled: bool
    noise_reduction_strength: int
    antenna: str
    captured_at: float
    device: str = "primary"
    sdrconnect_api_version: str = ""
    source: Literal["capture", "tune", "back", "forward", "profile", "observed"]

    @classmethod
    def from_props(
        cls,
        props: dict[str, object],
        *,
        source: str,
        api_version: str = "",
        at: float | None = None,
    ) -> "ReceiverState":
        missing = [spec.name for spec in RESTORABLE if spec.name not in props]
        if missing:
            raise ValueError(f"missing properties: {', '.join(missing)}")
        fields = {spec.field: props[spec.name] for spec in RESTORABLE}
        return cls(
            captured_at=time.time() if at is None else at,
            sdrconnect_api_version=api_version,
            source=source,
            **fields,
        )

    def to_props(self) -> dict[str, object]:
        return {spec.name: getattr(self, spec.field) for spec in RESTORABLE}

    def key(self) -> tuple:
        # Dwell identity: what "listening to something else" means. Gain/audio tweaks excluded.
        return (self.vfo_hz, self.demodulator, self.filter_bandwidth_hz, self.antenna)

class Observation(BaseModel):
    signal_power_db: float | None = None
    signal_snr_db: float | None = None
    overload: bool | None = None

class TuneRecipe(BaseModel):
    frequency_hz: int
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = None

class ProfileSettings(BaseModel):
    """Receiver regime; None means leave as is."""
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

    def to_props(self) -> dict[str, object]:
        return {
            FIELD_TO_PROP[f]: v
            for f, v in self.model_dump(exclude={"native_profile_name"}).items()
            if v is not None
        }
