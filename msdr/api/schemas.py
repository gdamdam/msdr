"""Request/response models for the browser API.

Pydantic here is fine -- `api/` is the one package allowed to import web
framework bits, and these models are the wire contract for `msdr/ui` (Task 9).
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from msdr.contracts.evidence import ClaimSubject
from msdr.engine.restore import ProfileStep, RestoreReport, SetStep, StepOutcome
from msdr.library.models import Satellite, SignalColor, TagColor
from msdr.settings import (
    VALID_WIFI_5GHZ_CHANNELS,
    BleSettings,
    DecoderSettings,
    ExtraSatellite,
    Features,
    HistorySettings,
    IntegrationsSettings,
    Layout,
    LnaAutoSettings,
    Location,
    NativeAudioSettings,
    SurveySettings,
    WifiSettings,
    WorkspaceId,
)

# Kept in lockstep with Signal.status (msdr/library/models.py) so an invalid
# value is a 422 at the request-parsing boundary, not a row that later blows
# up ReceiverProfile/Signal validation somewhere downstream (e.g. every
# signals.list() call, GET /api/state, every WS state push).
SignalStatus = Literal["unknown", "tentative", "identified"]


class SignalIn(BaseModel):
    name: str
    frequency_hz: int = Field(gt=0)
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = Field(default=None, gt=0)
    profile_id: str | None = None
    status: SignalStatus = "unknown"
    station: str = ""
    location: str = ""
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    urls: list[str] = []
    notes: str = ""
    tag_ids: list[str] = []
    color: SignalColor | None = None
    satellite: Satellite | None = None
    decoders: list[str] = []
    decoder_preset: Literal["meshcore_us", "meshtastic_us_longfast"] | None = None
    antenna: str | None = None
    lna_state: int | None = Field(default=None, ge=0, le=59)

    @model_validator(mode="after")
    def coordinates_are_paired(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class SignalPatch(BaseModel):
    # `name` is deliberately not nullable: Signal.name is a plain str, so
    # PATCH {"name": null} is a client mistake (422), not a request to wipe
    # it. exclude_unset means the default is never sent on an absent field.
    name: str = ""
    status: SignalStatus | None = None
    frequency_hz: int | None = Field(default=None, gt=0)
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = Field(default=None, gt=0)
    profile_id: str | None = None
    station: str | None = None
    location: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    urls: list[str] | None = None
    notes: str | None = None
    tag_ids: list[str] | None = None
    color: SignalColor | None = None
    satellite: Satellite | None = None
    decoders: list[str] | None = None
    decoder_preset: Literal["meshcore_us", "meshtastic_us_longfast"] | None = None
    antenna: str | None = None
    lna_state: int | None = Field(default=None, ge=0, le=59)


class ProfileIn(BaseModel):
    name: str
    description: str = ""
    native_profile_name: str | None = None
    antenna: str | None = None
    lna_state: int | None = None
    sample_rate_hz: int | None = Field(default=None, gt=0)
    squelch_enabled: bool | None = None
    squelch_threshold: float | None = None
    audio_agc_enabled: bool | None = None
    audio_agc_threshold: float | None = None
    noise_reduction_enabled: bool | None = None
    noise_reduction_strength: int | None = None


class ProfilePatch(BaseModel):
    name: str | None = None
    description: str | None = None
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


class TagIn(BaseModel):
    name: str
    color: TagColor | None = None


class TagPatch(BaseModel):
    name: str | None = None
    color: TagColor | None = None


class TuneFrequencyIn(BaseModel):
    hz: int
    # Optional so `{"hz": ...}` alone keeps working (e.g. journal-view/palette
    # "tune to this frequency" rows); set both to restore a full recipe (e.g.
    # a LISTEN beacon's CW/narrow-bandwidth tune target).
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = Field(default=None, gt=0)
    # Set by the MODE select: an explicit mode change goes through even while
    # settings.demodulator_locked keeps memories/profiles on the current mode.
    manual_mode: bool = False


class AntennaIn(BaseModel):
    antenna: str


class LnaIn(BaseModel):
    lna_state: int = Field(ge=0, le=59)


class SettingsPatch(BaseModel):
    evidence_panel_style: Literal["outline", "tinted"] | None = None
    dwell_seconds: float | None = None
    nearby_hz: int | None = None
    host: str | None = None
    port: int | None = None
    region: str | None = None
    nextpass_bin: str | None = None
    iqscan_bin: str | None = None
    iqscan_scan_root: str | None = None
    recorder_root: str | None = None
    passes_days: int | None = Field(default=None, ge=1, le=14)
    passes_min_elevation: float | None = Field(default=None, ge=0, le=90)
    passes_horizon: float | None = Field(default=None, ge=0, le=45)
    passes_top: int | None = Field(default=None, ge=1, le=200)
    passes_satellites: list[Satellite] | None = None
    passes_extra_satellites: list[ExtraSatellite] | None = None
    passes_visible_only: bool | None = None
    location: Location | None = None
    tle_cache_dir: str | None = None
    nextpass_cache_dir: str | None = None
    location_file: str | None = None
    doppler_min_step_hz: int | None = Field(default=None, ge=1, le=100_000)
    doppler_interval_s: float | None = Field(default=None, ge=0.25, le=60)
    # `Features | None`, but merged (not replaced wholesale) in
    # AppService.apply_settings: pydantic's exclude_unset propagates into
    # nested models, so a body like {"features": {"passes": false}} dumps to
    # exactly that single-key dict, not the model's other field defaults.
    features: Features | None = None
    catalog_sources: dict[str, str] | None = None
    catalog_languages: list[str] | None = None
    allocation_jurisdiction: Literal["US", "ITU1", "ITU2", "ITU3"] | None = None
    antenna_locked: bool | None = None
    demodulator_locked: bool | None = None
    lna_locked: bool | None = None
    allocation_data_dir: Path | None = None
    # `Layout | None`, but merged (not replaced wholesale) in
    # AppService.apply_settings, same as `features` above: a body like
    # {"layout": {"wide": [...]}} must leave `columns` as-is, and vice versa.
    # Kept for back-compat -- a pre-workspaces client's PATCH still targets
    # the active workspace (see AppService.apply_settings).
    layout: Layout | None = None
    # U2 workspaces: switch the active workspace, and/or merge a partial
    # Layout into one or more workspaces -- {"workspaces": {"discover":
    # {"wide": [...]}}} leaves every other workspace, and every other field
    # of "discover", untouched (same partial-merge reasoning as `layout`).
    workspace: WorkspaceId | None = None
    workspaces: dict[WorkspaceId, Layout] | None = None
    decoder: DecoderSettings | None = None
    wifi: WifiSettings | None = None
    ble: BleSettings | None = None
    history: HistorySettings | None = None
    native_audio: NativeAudioSettings | None = None
    integrations: IntegrationsSettings | None = None
    survey: SurveySettings | None = None
    # Shallow-merged like `features` (AppService.apply_settings); `bands`
    # is replaced as a whole list.
    lna_auto: LnaAutoSettings | None = None
    timezone: str | None = None


ListenKind = Literal["catalog", "signal", "pass"]

# What clicking a LISTEN row should actually restore. Distinct from
# ListenKind: a "pass" row's *id* is a synthetic `<sat>:<rise>` key (for
# display/dedup), but its *tune* target is the memory bound to that
# satellite, so the two kinds diverge for exactly that item type.
ListenTuneKind = Literal["catalog", "signal", "recipe"]


class ListenTune(BaseModel):
    kind: ListenTuneKind
    # catalog -> the catalog entry id; signal -> the signal id; recipe (e.g.
    # beacons) -> None, since there's no catalog/signal row to restore from.
    id: str | None = None
    frequency_hz: int
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = None


class ListenItem(BaseModel):
    kind: ListenKind
    id: str
    title: str
    subtitle: str
    frequency_hz: int
    demodulator: str | None = None
    when: str | None = None
    score: float = 0.0
    tune: ListenTune


class CatalogRefreshIn(BaseModel):
    source: str


class AllocationRefreshIn(BaseModel):
    source: str


class CatalogImportIn(BaseModel):
    path: str
    format: Literal["sdrpp", "gqrx"]


class ScanRunIn(BaseModel):
    # Exactly one of path/redetect -- enforced in AppService.run_scan, not
    # here, since it needs to turn into a specific 400 message.
    path: str | None = None
    sample_rate: int | None = None
    center_hz: int | None = None
    threshold: float | None = None
    # Raw-format hint for a .cu8/.cf32_be input; SigMF/WAV are auto-detected
    # by iqscan, so this is left None for those.
    format: Literal["cu8", "cf32_be"] | None = None
    channel_clips: bool = False
    # A previous scan's id, to rerun detection from its cached spectrum.npz
    # instead of a fresh recording (mutually exclusive with path).
    redetect: str | None = None


BulkOp = Literal[
    "delete", "tag_add", "tag_remove", "set_status", "set_color", "list_add", "list_remove"
]


class BulkOpIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    op: BulkOp
    # tag_add/tag_remove: a tag id. set_status: a SignalStatus. set_color: a
    # SignalColor, or None to clear. list_add/list_remove: a list id. delete: ignored.
    value: str | None = None


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ListPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    position: int | None = None


class ListItemsIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)


class ListOrderIn(BaseModel):
    ids: list[str] = Field(max_length=5000)


class MergeIn(BaseModel):
    keep_id: str
    merge_ids: list[str] = Field(min_length=1, max_length=100)


class ListItemSkipPatch(BaseModel):
    skip: bool


# --- scanner (v0.12.0) ---------------------------------------------------


class ScanListSourceIn(BaseModel):
    type: Literal["list"]
    list_id: str


class ScanRangeSourceIn(BaseModel):
    type: Literal["range"]
    start_hz: int = Field(gt=0)
    stop_hz: int = Field(gt=0)
    step_hz: int = Field(gt=0)
    demodulator: str | None = None
    filter_bandwidth_hz: int | None = Field(default=None, gt=0)


class ScanSpectrumSourceIn(BaseModel):
    type: Literal["spectrum"]
    start_hz: int | None = Field(default=None, gt=0)
    stop_hz: int | None = Field(default=None, gt=0)
    step_hz: int = Field(default=12_500, gt=0)
    level_threshold: int = 20
    demodulator: str = "NFM"
    filter_bandwidth_hz: int = Field(default=12_500, gt=0)

    @model_validator(mode="after")
    def _check_range(self) -> "ScanSpectrumSourceIn":
        if self.start_hz is not None and self.stop_hz is not None and self.start_hz >= self.stop_hz:
            raise ValueError("stop_hz must be > start_hz")
        return self


class ScanStartIn(BaseModel):
    source: ScanListSourceIn | ScanRangeSourceIn | ScanSpectrumSourceIn = Field(
        discriminator="type"
    )
    # None for dwell_ms means "use the source-appropriate default" (400 list /
    # 250 range) -- see msdr.engine.scanner.default_dwell_ms.
    dwell_ms: int | None = Field(default=None, gt=0)
    settle_ms: int | None = Field(default=None, ge=0)
    threshold_snr_db: float | None = None
    hang_s: float | None = Field(default=None, ge=0)
    stop_on_hit: bool = False
    capture_hits: bool = False
    # Scanner finds log (S11) -- see docs/superpowers/plans/2026-09-25-scanner-finds.md.
    log_finds: bool = False
    keep_scanning: bool = False
    finds_dwell_s: float = 3.0
    skip_ignored: bool = True


class ScanSkipIn(BaseModel):
    persist: bool = False


class FindPatchIn(BaseModel):
    status: str | None = None
    note: str | None = None


class FindPromoteIn(BaseModel):
    name: str | None = None


class RecorderArmIn(BaseModel):
    on: bool


class WifiStartIn(BaseModel):
    channel: int

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: int) -> int:
        if 1 <= value <= 14 or value in VALID_WIFI_5GHZ_CHANNELS:
            return value
        raise ValueError(f"invalid Wi-Fi channel: {value!r}")


# Wi-Fi channel survey (docs/history.md "Wi-Fi channel survey") dwell bounds
# as module constants rather than baked into Field(ge=, le=) -- validate_dwell_s
# below reads them at call time, so tests can monkeypatch a short floor
# instead of waiting out the real 5 s minimum per channel hop.
WIFI_SURVEY_MIN_DWELL_S = 5
WIFI_SURVEY_MAX_DWELL_S = 120


class WifiSurveyIn(BaseModel):
    channels: list[int] = Field(min_length=1, max_length=64)
    dwell_s: float = Field(default=10)
    rounds: int = Field(default=1, ge=0, le=100)

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: list[int]) -> list[int]:
        for ch in value:
            if not (1 <= ch <= 14 or ch in VALID_WIFI_5GHZ_CHANNELS):
                raise ValueError(f"invalid Wi-Fi channel: {ch!r}")
        return value

    @field_validator("dwell_s")
    @classmethod
    def validate_dwell_s(cls, value: float) -> float:
        if not (WIFI_SURVEY_MIN_DWELL_S <= value <= WIFI_SURVEY_MAX_DWELL_S):
            raise ValueError(
                f"dwell_s must be between {WIFI_SURVEY_MIN_DWELL_S} and {WIFI_SURVEY_MAX_DWELL_S}"
            )
        return value


class DecoderPrepareIn(BaseModel):
    decoder: str
    frequency_hz: int | None = Field(default=None, gt=0, le=10_000_000_000)
    lna_state: int | None = Field(default=None, ge=0, le=59)
    profile: Literal["meshcore_us", "meshtastic_us_longfast"] | None = None
    token: str | None = None


class DecoderRestoreIn(BaseModel):
    token: str


class LibraryRedoIn(BaseModel):
    id: str


class DecoderStartIn(BaseModel):
    decoders: list[str] = Field(min_length=1)
    # Tune here first when given (the signal panel's DECODE for a memory or
    # find that isn't the current VFO); otherwise decode whatever is tuned.
    frequency_hz: int | None = None


class BandStartIn(BaseModel):
    decoders: list[str] = Field(min_length=1)
    max_channels: int | None = None
    threshold_db: float | None = None


class BandSaveIn(BaseModel):
    frequency_hz: int
    name: str | None = None


class SurveyReplayIn(BaseModel):
    path: str  # SDRconnect IQ .wav, or an msdr SigMF recording (file or directory)
    loop: bool = True


class SurveyReplayControlIn(BaseModel):
    playing: bool | None = None
    position_s: float | None = None
    loop: bool | None = None


class SurveySpanIn(BaseModel):
    sample_rate_hz: int
    center_hz: int
    keep_vfo: bool = True


class SurveyDimensionsIn(BaseModel):
    dimensions: list[str]


class SurveyDecodeIn(BaseModel):
    frequency_hz: int
    # None: let Survey.decode() fall back to the label's own
    # family.suggested_decoders (409 when there aren't any).
    decoders: list[str] | None = None


class SurveyTestSignalIn(BaseModel):
    frequency_hz: int = Field(gt=0, le=6_000_000_000)
    decoders: list[str] = Field(default_factory=list, max_length=8)


class SurveyUndecodeIn(BaseModel):
    frequency_hz: int


class SurveySaveIn(BaseModel):
    frequency_hz: int
    name: str | None = None


class SurveyBandScanStartIn(BaseModel):
    start_hz: int = Field(gt=0, le=6_000_000_000)
    end_hz: int = Field(gt=0, le=6_000_000_000)
    span_hz: int = Field(gt=0, le=10_000_000)
    dwell_s: float = Field(gt=0, le=300)
    overlap: float = Field(ge=0, lt=0.9)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_hz <= self.start_hz:
            raise ValueError("end_hz must be greater than start_hz")
        return self


class BeaconStartIn(BaseModel):
    mode: Literal["ncdxf", "ndb"]
    frequencies_hz: list[int] | None = None


class SavedSearchIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    # Keys are validated against the LIBRARY query's own param whitelist in
    # AppService.create_saved_search, not here (the whitelist lives with the
    # route it mirrors).
    params: dict[str, object]


# --- observations and honest evidence (S2) --------------------------------


class ObservationPatch(BaseModel):
    # Each key acts only when present (model_fields_set): `signal_id: null`
    # unlinks, a signal id links, and a body with just `notes` leaves the
    # memory link alone. At least one key is required, as before S5 (when
    # signal_id itself was required).
    signal_id: str | None = None
    notes: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _one_key(self):
        if not self.model_fields_set & {"signal_id", "notes"}:
            raise ValueError("signal_id or notes required")
        if "notes" in self.model_fields_set and self.notes is None:
            raise ValueError("notes must be a string")
        return self


class ObservationPromoteIn(BaseModel):
    name: str | None = None


# --- source-independent playback / review (S3) ----------------------------


class ReviewTuneIn(BaseModel):
    frequency_hz: int
    demodulator: str
    bandwidth_hz: int = Field(gt=0)


class NativeAudioPatch(BaseModel):
    model_config = {"extra": "forbid"}
    agc_mode: Literal["default", "off", "fast", "slow"] | None = None
    manual_gain: float | None = Field(default=None, ge=0, le=20)
    ssb_low_hz: int | None = Field(default=None, ge=0, le=3000)
    ssb_high_hz: int | None = Field(default=None, ge=300, le=10000)
    cw_pitch_hz: int | None = Field(default=None, ge=300, le=1500)
    notch_enabled: bool | None = None
    notch_frequency_hz: int | None = Field(default=None, ge=100, le=5000)
    notch_width_hz: int | None = Field(default=None, ge=10, le=500)
    wfm_deemphasis_us: Literal[0, 50, 75] | None = None
    nfm_audio_mode: Literal["voice", "flat"] | None = None
    nr_enabled: bool | None = None
    nr_strength: float | None = Field(default=None, ge=0, le=1)
    blanker_enabled: bool | None = None
    blanker_threshold: float | None = Field(default=None, ge=4, le=20)

    @model_validator(mode="after")
    def valid_passband(self):
        if (
            self.ssb_low_hz is not None
            and self.ssb_high_hz is not None
            and self.ssb_high_hz - self.ssb_low_hz < 200
        ):
            raise ValueError("SSB passband must be at least 200 Hz wide")
        return self


class ReceiverAudioDspIn(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    property: str
    value: bool | int | float

    @field_validator("value", mode="before")
    @classmethod
    def validate_audio_dsp_value(cls, value):
        import math

        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class ReviewAudioIn(BaseModel):
    squelch_db: float | None = None
    volume: float | None = None
    mute: bool | None = None


class ReviewSeekIn(BaseModel):
    position_s: float


class ReviewLoopIn(BaseModel):
    loop: bool


class BookmarkIn(BaseModel):
    note: str = ""


class ClipIn(BaseModel):
    # Timeline-elapsed seconds on the running replay (same clock as
    # position_s); range checks are the service's (409 with a reason).
    start_s: float
    end_s: float
    note: str = ""


class ReviewDecodeIn(BaseModel):
    decoders: list[str] = Field(min_length=1)
    mode: Literal["single", "band"] = "single"
    max_channels: int | None = None
    threshold_db: float | None = None


class OperatorClaimIn(BaseModel):
    # The same subject vocabulary claims are stored under (msdr.contracts.
    # evidence.ClaimSubject) -- an unlisted subject is a 422 here for free.
    subject: ClaimSubject
    value: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    supersedes: str | None = None


def outcome_to_dict(outcome: StepOutcome) -> dict:
    step = outcome.step
    if isinstance(step, SetStep):
        entry = {"kind": "set", "property": step.property, "value": step.value}
    else:
        assert isinstance(step, ProfileStep)
        entry = {"kind": "profile", "name": step.name}
    entry["status"] = outcome.status
    entry["detail"] = outcome.detail
    return entry


def restore_report_to_dict(report: RestoreReport) -> dict:
    """Serialise a RestoreReport (a dataclass, not a pydantic model)."""
    return {
        "status": report.status,
        "outcomes": [outcome_to_dict(o) for o in report.outcomes],
        "duration_s": report.duration_s,
        "detail": report.detail,
    }


class LabelIn(BaseModel):
    """PUT /api/labels/{receiver}/{ident} (docs/history.md "Labels"); lengths
    and class are validated by LabelsRepo so the message is the same for
    every caller."""

    name: str
    note: str = ""
    model_config = ConfigDict(populate_by_name=True)
    cls: str = Field(alias="class")


class RuleIn(BaseModel):
    """POST /api/rules (docs/rules.md). `config`'s shape depends on `kind`
    and is validated live by RulesServiceMixin (it needs the live BLE/Wi-Fi
    tables, e.g. to refuse a rotating BLE address), not here."""

    kind: Literal["beacon_presence", "sensor_threshold", "drone_geofence"]
    name: str
    config: dict
    enabled: bool = True
    cooldown_s: float = Field(default=60.0, ge=0)


class RulePatch(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None
    cooldown_s: float | None = Field(default=None, ge=0)
