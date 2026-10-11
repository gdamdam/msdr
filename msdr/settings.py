"""Application settings, persisted as JSON under the platform config dir."""

import errno
import functools
import json
import os
import re
import tempfile
import time
from datetime import UTC, timedelta, tzinfo
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from msdr.library.models import Satellite

DEFAULT_SETTINGS_PATH = Path.home() / "Library/Application Support/msdr/settings.json"

# Known section ids, in the order they currently render (msdr/ui/app.js) --
# the source of truth for Layout validation and the default below.
SECTION_IDS = (
    "survey",
    "nearby",
    "inbox",
    "recent",
    "listen",
    "log",
    "passes",
    "sky",
    "scans",
    "catalog",
    "beacons",
    "finds",
    # S1 (0.26.0, docs/epics/listening-workbench/S1-ui-contract.md): the
    # scanner/decode decks moved out of the instrument panel into sections of
    # their own, plus the REVIEW shell and LIBRARY-as-a-section. Appended (not
    # interleaved) so the backfill order of every existing id is unchanged.
    "scan",
    "decode",
    "review",
    "library",
    # 0.53.0: the ALLOCATIONS workspace's chart section (docs/allocations.md).
    "allocations",
)
_SECTION_ID_SET = set(SECTION_IDS)

# Section ids that used to be valid but no longer render as a grid section --
# "scanner" moved into the instrument panel as a compact deck in v0.14.2. A
# saved layout.columns from before that (v0.12.0-0.14.1) still has "scanner"
# in it; the Layout validator below drops these silently rather than 422ing,
# while a genuinely unknown id still raises.
_RETIRED_SECTION_IDS = frozenset({"scanner"})


class Location(BaseModel):
    lat: float
    lon: float
    altitude_m: float = 0
    timezone: str = "UTC"


class ExtraSatellite(BaseModel):
    """A user-added PASSES satellite, tracked by NORAD id rather than the
    closed `Satellite` label vocabulary (see passes_extra_satellites) --
    additive, and kept separate so the Satellite Literal (passes_satellites,
    Signal.satellite) never has to widen for backward compatibility."""

    label: str
    norad_id: int


class Features(BaseModel):
    """Per-feature on/off switches, with channel scanner on by default. `state_payload()`
    exposes these so the UI can hide sections; the backend itself honours
    the ones with real side effects (see AppService)."""

    detect: bool = True
    ident: bool = True
    nearby: bool = True
    journal_passive: bool = True
    passes: bool = True
    scans: bool = True
    sky: bool = True
    tracking: bool = True
    catalog: bool = True
    # S-meter telemetry pushed over the websocket (a couple of frames a
    # second); off means the panel only updates when something else does.
    telemetry: bool = True
    # Server-side scanner engine (list/range sources sweeping the VFO on
    # their own -- v0.12.0).
    scanner: bool = True
    # Pre-roll IQ recorder (own SDRconnect stream, rolling buffer + REC).
    recorder: bool = True
    # Signal decoders (msdr.decoder): memory-driven multimon-ng/minimodem
    # decoding of SDRconnect's demodulated audio (P1).
    decoder: bool = True
    # Wide-view survey (msdr.survey, S1): span picker, live spectrum +
    # waterfall, occupancy over the SpectrumFrame stream.
    survey: bool = True
    # Beacon monitor (msdr.survey.beacons, S10): cycles the receiver over
    # NCDXF/IARU or NDB beacon frequencies logging SNR/CW-decoded hits.
    beacons: bool = True
    # Scanner finds log (S11): every hit recorded as a find when the scan's
    # log_finds option is on -- see msdr.api.service's on_find wiring.
    finds: bool = True


class DecoderSettings(BaseModel):
    """Signal decoder configuration (msdr.decoder, P1) -- see
    docs/superpowers/specs/2026-09-25-decoders-design.md."""

    bin_dirs: list[str] = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    history_limit: int = 5000
    # BAND DECODE (P2) defaults -- see docs/superpowers/plans/2026-09-25-decoders-p2-band.md.
    band_max_channels: int = 8
    band_threshold_db: float = 10.0
    band_idle_s: int = 60
    band_spacing_hz: int = 12500
    # Per-call WAV storage for call-kind decoders (dsd-fme DMR/P25/NXDN, S6).
    # None resolves to <db_path's dir>/decoder/calls (see AppService._build_decoder).
    calls_dir: str | None = None
    calls_keep: int = 500
    # Progressive PNG storage for image-kind decoders (wefax, S7-S10 round 1).
    # None resolves to <db_path's dir>/decoder/fax (see AppService._build_decoder).
    fax_dir: str | None = None
    fax_keep: int = 200
    # LoRa receive profile for the raw-IQ decoder. Coding rate n means 4/(4+n).
    lora_spreading_factor: int = Field(default=11, ge=7, le=12)
    lora_bandwidth_hz: Literal[62500, 125000, 250000, 500000] = 250000
    lora_coding_rate: int = Field(default=1, ge=1, le=4)
    lora_protocol: Literal["lora", "meshtastic", "meshcore"] = "lora"
    # NFM-family single-channel decodes run through the IQ path by default:
    # SDRconnect's audio chain (DC block / de-emphasis) deterministically
    # garbles the first POCSAG codeword, the IQ discriminator does not.
    # "audio" is the escape hatch.
    single_channel_source: Literal["iq", "audio"] = "iq"


class JS8CallIntegrationSettings(BaseModel):
    """Read-only connection to JS8Call's local TCP Network Message API."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=2242, ge=1, le=65535)


class PatIntegrationSettings(BaseModel):
    """Read-only connection to Pat's built-in Winlink HTTP interface."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Pat URL must be a base http(s) URL without credentials or a path")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("Pat URL has an invalid port") from exc
        return value.strip().rstrip("/")


class FreeDATAIntegrationSettings(BaseModel):
    """Read-only connection to FreeDATA's HTTP server."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:5000"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        from msdr.integrations.freedata import normalize_base_url

        return normalize_base_url(value)


class IntegrationsSettings(BaseModel):
    """Optional links to separately-running digital-mode applications."""

    js8call: JS8CallIntegrationSettings = Field(default_factory=JS8CallIntegrationSettings)
    pat: PatIntegrationSettings = Field(default_factory=PatIntegrationSettings)
    freedata: FreeDATAIntegrationSettings = Field(default_factory=FreeDATAIntegrationSettings)



class SpanPreset(BaseModel):
    """A one-tap span for msdr.survey's span picker (S1)."""

    name: str
    sample_rate_hz: int
    center_hz: int


class SurveySettings(BaseModel):
    """Wide-view survey configuration (msdr.survey, S1) -- see
    docs/superpowers/specs/2026-09-25-wide-view-identification-design.md."""

    threshold_db: float = 8.0
    occupancy_windows_s: tuple[int, int] = (60, 600)
    spectrum_fps: int = 10
    peak_threshold_db: float = Field(default=10.0, ge=0, le=60)
    # Optional SDR receiver DC guard. Off keeps the true center bin measurable.
    center_exclusion_enabled: bool = False
    marker_hold_s: float = Field(default=15.0, ge=0, le=60)
    label_ttl_s: int = 120
    # A label the survey recognised (analyzer family, RDS station name or a
    # confirmed decode) outlives label_ttl_s: it expires after this many
    # seconds unseen instead -- None keeps it for the whole session.
    recognized_ttl_s: int | None = None
    dimensions: list[str] = Field(
        default_factory=lambda: ["activity", "bandwidth", "bandplan", "catalog"]
    )
    # IQ analyzer (S3/S4) -- see msdr.survey.analyzer.Analyzer.
    max_analyzed: int = 6
    analyze_dwell_s: float = 2.0
    analyze_min_snr_db: float = 8.0
    # RDS: snap FM stations to the odd-tenth 200 kHz grid (88.1, 88.3, ...),
    # the Americas' allocation -- even-tenth "channels" there are HD Radio
    # sidebands/side peaks, each costing an RDS dwell. Off: the 100 kHz grid
    # that also fits Europe's 100 kHz spacing.
    rds_odd_channels: bool = False
    # Waterfall/spectrum display defaults (SETUP's Waterfall group) -- these
    # only seed survey-view.js's on-canvas controls; a per-browser localStorage
    # override (msdr.survey.wf) still wins once the operator touches MIN/MAX,
    # until RESET clears it. wf_min_db/wf_max_db of None means "auto from the
    # stream's base_db/ref_level_db", today's behaviour.
    wf_min_db: float | None = None
    wf_max_db: float | None = None
    wf_avg_factor: float = Field(default=0.5, ge=0, le=0.95)
    wf_peak_hold: bool = False
    wf_palette: Literal["sdrsharp", "satdump", "gray"] = "sdrsharp"
    wf_history_rows: int = Field(default=300, ge=50, le=2000)
    wf_show_grid: bool = True
    presets: list[SpanPreset] = Field(
        default_factory=lambda: [
            SpanPreset(name="VHF 152 pagers", sample_rate_hz=2_000_000, center_hz=152_500_000),
            SpanPreset(name="929-932 pagers", sample_rate_hz=4_000_000, center_hz=930_500_000),
            SpanPreset(name="Airband", sample_rate_hz=8_000_000, center_hz=127_000_000),
            SpanPreset(name="2 m", sample_rate_hz=4_000_000, center_hz=146_000_000),
            SpanPreset(name="Marine", sample_rate_hz=4_000_000, center_hz=158_000_000),
        ]
    )

    @model_validator(mode="after")
    def _validate_wf_range(self) -> "SurveySettings":
        min_db, max_db = self.wf_min_db, self.wf_max_db
        if min_db is not None and max_db is not None and min_db >= max_db:
            raise ValueError("survey.wf_min_db must be less than survey.wf_max_db")
        return self


class BeaconSettings(BaseModel):
    """Beacon monitor configuration (msdr.survey.beacons, S10) -- see
    docs/superpowers/plans/2026-09-25-survey-s10-beacons.md."""

    snr_threshold_db: float = 6.0
    ndb_frequencies_hz: list[int] = []
    ndb_dwell_s: int = 30
    # NCDXF/IARU beacon network's 5 fixed frequencies -- duplicated from
    # msdr.survey.beacons.NCDXF_BANDS_HZ (not imported: msdr.settings has no
    # dependency on msdr.survey, matching SurveySettings' own convention).
    band_order_hz: list[int] = Field(
        default_factory=lambda: [14_100_000, 18_110_000, 21_150_000, 24_930_000, 28_200_000]
    )


class Layout(BaseModel):
    """Which of the 3 UI columns each section renders in, and in what order
    (msdr/ui/app.js drag-to-rearrange) -- PATCHed (merged, not wholesale) via
    SettingsPatch.layout. The default must match msdr/ui/format.js's
    DEFAULT_LAYOUT_COLUMNS exactly (see that file's LAYOUT_PRESETS and
    tests asserting this default). A missing id (a section added after this
    layout was saved) is appended to the last column rather than silently
    never appearing.

    `wide` lists section ids that render full-width in a band above the
    grid, in order. An id in `wide` still keeps its slot in `columns` (old
    clients that don't know about `wide` just render the grid as before;
    the UI is the one that skips `wide` ids in the grid)."""

    # S1: this is the LISTEN workspace's default (the legacy `layout` field
    # mirrors the active workspace, "listen" by default). It no longer names
    # every id -- the validator backfills the rest into the last column, the
    # same rule that migrates an old saved layout.
    columns: list[list[str]] = Field(
        default_factory=lambda: [
            ["survey", "listen", "decode"],
            ["nearby", "recent"],
            ["log"],
        ]
    )
    wide: list[str] = Field(default_factory=lambda: ["survey"])

    @model_validator(mode="after")
    def _validate_and_backfill(self) -> "Layout":
        if len(self.columns) != 3:
            raise ValueError("layout.columns must have exactly 3 columns")
        # Drop retired ids (e.g. "scanner") before validating -- an existing
        # settings.json that still has one must keep loading. Only reassign
        # when something actually changed: an unconditional `self.x = ...`
        # here would mark the field "set" even when the caller never
        # touched it, breaking SettingsPatch's exclude_unset-based partial
        # merge (see AppService.apply_settings).
        filtered_columns = [
            [sid for sid in col if sid not in _RETIRED_SECTION_IDS] for col in self.columns
        ]
        if filtered_columns != self.columns:
            self.columns = filtered_columns
        filtered_wide = [sid for sid in self.wide if sid not in _RETIRED_SECTION_IDS]
        if filtered_wide != self.wide:
            self.wide = filtered_wide
        seen: set[str] = set()
        for col in self.columns:
            for section_id in col:
                if section_id not in _SECTION_ID_SET:
                    raise ValueError(f"unknown layout section id: {section_id!r}")
                if section_id in seen:
                    raise ValueError(f"duplicate layout section id: {section_id!r}")
                seen.add(section_id)
        missing = [sid for sid in SECTION_IDS if sid not in seen]
        if missing:
            self.columns[-1].extend(missing)
        seen_wide: set[str] = set()
        for section_id in self.wide:
            if section_id not in _SECTION_ID_SET:
                raise ValueError(f"unknown layout section id: {section_id!r}")
            if section_id in seen_wide:
                raise ValueError(f"duplicate layout section id: {section_id!r}")
            seen_wide.add(section_id)
        return self


# U2 workspaces (docs/superpowers/specs/2026-09-26-ui-rework-design.md, "Additions from the
# second review note"): named, remembered arrangements the UI switches between with a top-level
# tab strip. Each is a full `Layout` -- like the single legacy `layout` field, an id not placed in
# any of a workspace's columns is backfilled into its last column by Layout's own validator above,
# so a workspace's *raw* stored Layout ends up covering all of SECTION_IDS. WORKSPACE_SECTIONS
# below is the UI's separate, fixed notion of which ids are actually that workspace's own -- it
# decides what renders, ignoring the backfilled tail; it partitions SECTION_IDS between the eight
# workspaces so nothing is ever unreachable. "listen" holds whatever the
# other destinations don't claim
# (see the assertion below).
# Tuple order is the tab order (S1 contract). "survey" is claimed by discover here only to keep the
# partition exact -- the UI renders it once in a fixed primary slot for both listen and discover.
WorkspaceId = Literal[
    "listen", "discover", "map", "band_scans", "review", "library", "satellites", "catalog",
    "allocations",
]
WORKSPACE_IDS: tuple[WorkspaceId, ...] = (
    "listen", "discover", "map", "band_scans", "review", "library", "satellites", "catalog",
    "allocations",
)

_NON_LISTEN_WORKSPACE_SECTIONS: dict[str, tuple[str, ...]] = {
    "discover": ("survey", "scan", "finds", "inbox", "scans", "beacons"),
    "map": (),
    "band_scans": (),
    "review": ("review",),
    # Unidentified saves live beside scanner finds in Discover → Inbox.
    "library": ("library",),
    "satellites": ("passes", "sky"),
    "catalog": ("catalog",),
    "allocations": ("allocations",),
}
WORKSPACE_SECTIONS: dict[str, tuple[str, ...]] = {
    "listen": tuple(
        sid
        for sid in SECTION_IDS
        if sid not in {s for ids in _NON_LISTEN_WORKSPACE_SECTIONS.values() for s in ids}
    ),
    **_NON_LISTEN_WORKSPACE_SECTIONS,
}
assert sorted(s for ids in WORKSPACE_SECTIONS.values() for s in ids) == sorted(SECTION_IDS)

# Discover/Review/Library/Satellites' own default arrangement of just their WORKSPACE_SECTIONS ids
# (Layout's validator backfills the rest into the last column, invisible in the new UI -- see
# above). "listen" is deliberately `Layout()` itself: the old `layout` field's default must keep
# matching msdr/ui/format.js's DEFAULT_LAYOUT_COLUMNS, which is listen's default.
_WORKSPACE_DEFAULT_COLUMNS: dict[str, list[list[str]]] = {
    "discover": [["survey"], ["scan", "finds", "inbox"], ["scans", "beacons"]],
    "map": [[], [], []],
    "band_scans": [[], [], []],
    "review": [["review"], [], []],
    "library": [["library"], [], []],
    "satellites": [["passes"], ["sky"], []],
    "catalog": [["catalog"], [], []],
    "allocations": [["allocations"], [], []],
}
_WORKSPACE_DEFAULT_WIDE: dict[str, list[str]] = {
    "discover": ["survey", "scan", "finds"],
    "map": [],
    "band_scans": [],
    "review": ["review"],
    "library": ["library"],
    "satellites": [],
    "catalog": ["catalog"],
    "allocations": ["allocations"],
}


def _default_workspaces() -> dict[str, Layout]:
    workspaces: dict[str, Layout] = {"listen": Layout()}
    for ws in _WORKSPACE_DEFAULT_COLUMNS:
        workspaces[ws] = Layout(
            columns=[list(col) for col in _WORKSPACE_DEFAULT_COLUMNS[ws]],
            wide=list(_WORKSPACE_DEFAULT_WIDE[ws]),
        )
    return workspaces


#: Valid 5 GHz 20 MHz-wide channel numbers (docs/wifi.md: "36-177 valid 20
#: MHz channels") -- the regulatory channel grid across the U-NII bands.
VALID_WIFI_5GHZ_CHANNELS = frozenset(
    {
        36, 40, 44, 48, 52, 56, 60, 64,
        100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
        149, 153, 157, 161, 165, 169, 173, 177,
    }
)


class WifiSettings(BaseModel):
    """Passive Wi-Fi reception (AWUS036ACH), see docs/wifi.md. The helper
    subprocess (msdr-wifi-capture) is GPL-2.0 code kept entirely outside
    this repo -- msdr only starts it and reads pcap/JSON from it
    (msdr.wifi.supervisor.WifiSupervisor)."""

    enabled: bool = False
    # None resolves to ~/dev/radio/rtl8812au-helper/.venv/bin/msdr-wifi-capture
    # (see msdr.wifi.supervisor.DEFAULT_HELPER_PATH).
    helper_path: str | None = None
    # Test-only escape hatch: an explicit argv (e.g. [sys.executable,
    # "fake_helper.py"]) in place of resolving helper_path -- never set by
    # the UI/PATCH, only by tests (tests/wifi/fake_helper.py).
    helper_command: list[str] | None = None
    channel: int = 6
    record_max_mb: int = Field(default=200, ge=1)
    record_max_minutes: int = Field(default=60, ge=1)
    retention_days: int = Field(default=14, ge=1)

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: int) -> int:
        if 1 <= value <= 14 or value in VALID_WIFI_5GHZ_CHANNELS:
            return value
        raise ValueError(f"invalid Wi-Fi channel: {value!r}")


#: `vid:pid` (4 hex digits each), e.g. "19ff:0239" -- docs/ble.md's USB id
#: shape for the optional BLE dongle selector.
_BLE_USB_ID_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$")


class HistorySettings(BaseModel):
    """Device history for the Wi-Fi and BLE receivers (docs/history.md):
    per-minute buckets in a separate database next to msdr.db, kept out of
    backups because it is bulk, regenerable observation data."""

    # Off by default: it keeps a per-minute log of other people's devices
    # (docs/ble.md "Passive listening only, and privacy"). Turned on by
    # setting `history.enabled: true` in settings.json; there is no UI toggle.
    enabled: bool = False
    retention_days: int = Field(default=30, ge=1, le=3650)
    # Wi-Fi activity heatmap (docs/wifi.md "Wi-Fi activity"): per-second,
    # per-channel rows are ~86,400/day per channel -- far bulkier than the
    # per-minute `buckets`/`coverage` rows, so they get their own, shorter cap.
    activity_retention_hours: int = Field(default=48, ge=1, le=720)
    # None resolves to <db_path's dir>/history.db (":memory:" when db_path is).
    db_path: Path | None = None


class BleSettings(BaseModel):
    """Passive BLE reception, see docs/ble.md. `backend="hci"` drives a
    Bluetooth 4.x USB dongle over raw HCI (`python -m msdr.ble.capture`);
    `backend="sniffle"` drives a SONOFF ZBDongle-P (CC2652P) running Sniffle
    firmware over serial (`python -m msdr.ble.sniffle_capture`). Either way
    the helper is fed to an in-memory `AdvertiserTable` by
    `msdr.ble.supervisor.BleSupervisor`; nothing here is persisted beyond
    the current table (no sessions/recording in this release)."""

    enabled: bool = False
    backend: Literal["hci", "sniffle"] = "hci"
    usb_id: str | None = None  # hci backend only
    serial_port: str | None = None  # sniffle backend only
    # None = auto (try 2000000, then 921600 -- CP2102 vs CP2102N, docs/ble.md).
    baud: int | None = None
    phy: Literal["1m", "2m", "coded"] = "1m"
    channel: Literal["37", "38", "39", "hop"] = "37"
    # Test-only escape hatch: an explicit argv (e.g. [sys.executable,
    # "fake_helper.py"]) in place of resolving the real helper module --
    # never set by the UI/PATCH, only by tests (tests/ble/fake_helper.py,
    # tests/ble/fake_sniffle.py).
    helper_command: list[str] | None = None

    @field_validator("usb_id")
    @classmethod
    def validate_usb_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not _BLE_USB_ID_RE.match(value):
            raise ValueError(f"invalid BLE USB id: {value!r}")
        return value.lower()

    @field_validator("serial_port")
    @classmethod
    def validate_serial_port(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("baud")
    @classmethod
    def validate_baud(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("BLE baud rate must be positive")
        return value


class NativeAudioSettings(BaseModel):
    """Native replay/workbench receiver controls; independent of SDRconnect."""

    agc_mode: Literal["default", "off", "fast", "slow"] = "default"
    manual_gain: float = Field(default=1.0, ge=0.0, le=20.0)
    ssb_low_hz: int = Field(default=0, ge=0, le=3000)
    ssb_high_hz: int | None = Field(default=None, ge=300, le=10_000)
    cw_pitch_hz: int = Field(default=700, ge=300, le=1500)
    notch_enabled: bool = False
    notch_frequency_hz: int = Field(default=1000, ge=100, le=5000)
    notch_width_hz: int = Field(default=100, ge=10, le=500)
    wfm_deemphasis_us: Literal[0, 50, 75] = 75
    nfm_audio_mode: Literal["voice", "flat"] = "voice"
    nr_enabled: bool = False
    nr_strength: float = Field(default=0.5, ge=0, le=1)
    blanker_enabled: bool = False
    blanker_threshold: float = Field(default=8.0, ge=4, le=20)

    @model_validator(mode="after")
    def validate_passband(self):
        if self.ssb_high_hz is not None and self.ssb_high_hz - self.ssb_low_hz < 200:
            raise ValueError("SSB passband must be at least 200 Hz wide")
        return self


class Settings(BaseModel):
    # Presentation of signal evidence panels in the browser UI.
    evidence_panel_style: Literal["outline", "tinted"] = "tinted"
    host: str = "127.0.0.1"
    port: int = 5454
    db_path: Path = Path.home() / "Library/Application Support/msdr/msdr.db"
    dwell_seconds: float = 8.0
    nearby_hz: int = 5000
    web_port: int = 8765
    region: str = "US"
    nextpass_bin: str = "nextpass"
    iqscan_bin: str = "iqscan"
    iqscan_scan_root: str = "~/dev/radio/iqscan/scans"
    passes_days: int = Field(default=2, ge=1, le=14)
    passes_min_elevation: float = Field(default=20.0, ge=0, le=90)
    passes_horizon: float = Field(default=10.0, ge=0, le=45)
    passes_top: int = Field(default=50, ge=1, le=200)
    # Explicit subset to predict passes for; empty means derive from bound
    # memories (or the full catalog when none are pinned) -- see
    # AppService.passes.
    passes_satellites: list[Satellite] = []
    # Additive to passes_satellites (or the bound-memory/full-catalog default
    # when that's empty) -- see AppService._passes_satellites.
    passes_extra_satellites: list[ExtraSatellite] = []
    # Filter to passes with a sunlit portion in dark sky. Cached visibility
    # is annotated even with filtering off; explicit refresh fetches the
    # ephemeris when this filter is enabled.
    passes_visible_only: bool = False
    location: Location | None = None
    tle_cache_dir: str = "~/Library/Application Support/msdr/tle"
    nextpass_cache_dir: str = "~/.cache/nextpass"
    location_file: str = "~/.config/radio/location.json"
    doppler_min_step_hz: int = Field(default=100, ge=1, le=100_000)
    doppler_interval_s: float = Field(default=2.0, ge=0.25, le=60)
    features: Features = Field(default_factory=Features)
    # Empty url for "eibi" means derive one from the current EiBi season
    # (see msdr.catalog.parsers.eibi.default_url/season_code). "aoki"/"hfcc"
    # have no default url -- blank means that source is left disabled.
    catalog_sources: dict[str, str] = Field(default_factory=lambda: {"eibi": "", "ndxc": ""})
    # EiBi language codes the user cares about; consumed later by LISTEN.
    catalog_languages: list[str] = []
    # Frequency allocation reference (docs/allocations.md): which table a
    # lookup resolves against. `region` keeps its own meaning for identify.
    allocation_jurisdiction: Literal["US", "ITU1", "ITU2", "ITU3"] = "US"
    # ANT lock (instrument panel): when set, restores (memories, profiles,
    # Back/Forward) never switch the antenna; only the ANT select does.
    antenna_locked: bool = False
    # MODE lock, same idea: restores keep the current demodulator (e.g. SAM
    # over a memory saved as AM); only the MODE select changes it.
    demodulator_locked: bool = False
    # LNA lock, same idea: restores keep the current LNA state; only the LNA
    # field on the instrument panel changes it.
    lna_locked: bool = False
    # None: AllocationStore's default (~/Library/Application Support/msdr/
    # allocations), where Settings -> Refresh writes the imported dataset.
    allocation_data_dir: Path | None = None
    # Kept for back-compat -- a pre-workspaces client reads/writes only this
    # field. It's a derived mirror of `workspaces[workspace]`, recomputed by
    # `_finalize_workspaces` below on every load/validate; a value set here
    # directly (e.g. by a PATCH body) is not what ends up stored -- see
    # AppService.apply_settings, which folds a `layout` patch into the active
    # workspace's entry in `workspaces` before validation.
    layout: Layout = Field(default_factory=Layout)
    # U2 workspaces -- the switch under the instrument panel. Each entry is a
    # full Layout (see WORKSPACE_SECTIONS/_default_workspaces above); `layout`
    # above always mirrors `workspaces[workspace]`.
    workspace: WorkspaceId = "listen"
    workspaces: dict[WorkspaceId, Layout] = Field(default_factory=_default_workspaces)
    # Pre-roll IQ recorder (msdr.recorder): its own SDRconnect stream keeps a
    # rolling on-disk buffer while armed; REC promotes it into a recording.
    # `recorder_root` holds preroll/ (disposable) and recordings/ (kept,
    # SigMF -- iqscan reads it directly). `recorder_armed` is remembered
    # across restarts and re-applied once connected+started (AppService).
    recorder_root: str = "~/Library/Application Support/msdr"
    recorder_preroll_minutes: float = Field(default=5, ge=1, le=30)
    recorder_min_free_gb: float = Field(default=20, ge=1)
    recorder_max_recording_minutes: float = Field(default=60, ge=1, le=600)
    recorder_armed: bool = False
    # Review clips (S5): extracted sample ranges of a recording, one SigMF
    # directory each. None means `<recorder_root>/clips`.
    clips_dir: str | None = None
    decoder: DecoderSettings = Field(default_factory=DecoderSettings)
    wifi: WifiSettings = Field(default_factory=WifiSettings)
    ble: BleSettings = Field(default_factory=BleSettings)
    history: HistorySettings = Field(default_factory=HistorySettings)
    native_audio: NativeAudioSettings = Field(default_factory=NativeAudioSettings)
    integrations: IntegrationsSettings = Field(default_factory=IntegrationsSettings)
    survey: SurveySettings = Field(default_factory=SurveySettings)
    beacons: BeaconSettings = Field(default_factory=BeaconSettings)
    # IANA zone name (e.g. "Europe/Rome") for displaying CATALOG's UTC HHMM
    # schedule windows in local time too. None means auto-detect -- see
    # detect_timezone()/effective_timezone() below and AppService.state_payload().
    timezone: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _seed_workspaces_from_legacy_layout(cls, data):
        # A settings.json saved before workspaces existed has a top-level
        # `layout` and no `workspaces` at all -- seed "listen" from it (the
        # other three are left for the field default to fill in below) so an
        # existing arrangement isn't lost on first load with this version.
        if isinstance(data, dict) and not data.get("workspaces") and data.get("layout"):
            data = dict(data)
            data["workspaces"] = {"listen": data["layout"]}
        return data

    @model_validator(mode="after")
    def _finalize_workspaces(self) -> "Settings":
        # A partial `workspaces` (e.g. just-migrated "listen" above, or a
        # hand-edited settings.json) is filled in with the usual defaults for
        # whichever of the five ids are still missing (e.g. "review" in a
        # settings.json saved before S1).
        defaults = None
        for ws in WORKSPACE_IDS:
            if ws not in self.workspaces:
                defaults = defaults or _default_workspaces()
                self.workspaces[ws] = defaults[ws]
        # `layout` is a pure mirror of the active workspace -- see its field
        # comment above.
        self.layout = self.workspaces[self.workspace]
        return self

    @model_validator(mode="after")
    def _validate_timezone(self) -> "Settings":
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown timezone: {self.timezone!r}") from exc
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or DEFAULT_SETTINGS_PATH
        backup = path.with_name(path.name + ".bak")
        for candidate in (path, backup):
            try:
                return cls.model_validate(json.loads(candidate.read_text()))
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError, ValidationError):
                # A previous atomic save may have been interrupted before the
                # replacement. Keep startup available and try the last known
                # good settings file next.
                continue
        return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or DEFAULT_SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + ".bak")
        if path.exists():
            previous = path.read_bytes()
            try:
                type(self).model_validate(json.loads(previous))
            except (json.JSONDecodeError, ValidationError):
                # The primary may already be damaged. Keep the last known
                # good backup intact rather than rotating corruption over it.
                pass
            else:
                _atomic_write(backup, previous)
        _atomic_write(path, self.model_dump_json(indent=2).encode("utf-8"))


def _atomic_write(path: Path, contents: bytes) -> None:
    """Replace a file atomically, keeping its temporary file on the same filesystem."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the directory entry where the filesystem supports it. Some
        # platforms and filesystems reject opening or syncing directories;
        # the file itself is already atomically replaced at this point.
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            errno.EBADF,
            errno.EACCES,
            errno.EPERM,
        }
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
        else:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                if exc.errno not in unsupported:
                    raise
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# "UTC+02:00" / "UTC-05:30" -- detect_timezone()'s last-resort fallback name,
# a fixed offset rather than a real IANA zone (no DST rules), so
# effective_timezone() below must special-case it rather than handing it to
# ZoneInfo (which would raise).
_FIXED_OFFSET_RE = re.compile(r"^UTC(?P<sign>[+-])(?P<hh>\d{2}):(?P<mm>\d{2})$")


def detect_timezone() -> str:
    """Best-effort local timezone name. Tried in order: the `TZ` env var (if
    it names a real IANA zone), the target of the `/etc/localtime` symlink
    (macOS/Linux, e.g. ".../zoneinfo/Europe/Rome" -> "Europe/Rome"), then a
    fixed "UTC+HH:MM" offset name derived from time.timezone/altzone. Never
    raises -- falls back to "UTC" if nothing else resolved."""
    tz_env = os.environ.get("TZ")
    if tz_env:
        try:
            ZoneInfo(tz_env)
            return tz_env
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        localtime_path = Path("/etc/localtime")
        if localtime_path.is_symlink():
            target = os.readlink(localtime_path)
            marker = "zoneinfo/"
            idx = target.rfind(marker)
            if idx != -1:
                name = target[idx + len(marker) :]
                try:
                    ZoneInfo(name)
                    return name
                except (ZoneInfoNotFoundError, ValueError):
                    pass
    except OSError:
        pass
    try:
        is_dst = bool(time.daylight and time.localtime().tm_isdst > 0)
        # time.timezone/altzone are seconds *west* of UTC (POSIX sign) --
        # negate to get the conventional UTC+HH:MM direction.
        offset_s = -(time.altzone if is_dst else time.timezone)
        if offset_s == 0:
            return "UTC"
        sign = "+" if offset_s > 0 else "-"
        hours, minutes = divmod(abs(offset_s) // 60, 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    except Exception:
        return "UTC"


def effective_timezone(settings: Settings) -> tuple[str, tzinfo]:
    """Resolve settings.timezone (None -> detect_timezone()) to a (name,
    tzinfo) pair for computing local offsets/times."""
    name = settings.timezone if settings.timezone is not None else detect_timezone()
    match = _FIXED_OFFSET_RE.match(name)
    if match:
        delta = timedelta(hours=int(match["hh"]), minutes=int(match["mm"]))
        if match["sign"] == "-":
            delta = -delta
        return name, dt_timezone(delta)
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC", UTC


@functools.lru_cache(maxsize=1)
def available_timezone_names() -> list[str]:
    """Sorted IANA zone names for the /api/timezones route -- cached since
    zoneinfo.available_timezones() re-scans the tzdata directory (~600 names)
    on every call."""
    return sorted(available_timezones())
