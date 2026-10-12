"""Property registry: wire names <-> ReceiverState fields, decode/encode, restore order.

Single source of truth for every SDRconnect property msdr knows about. Restore
staging follows the restore design (antenna before sample rate before frequencies
before mode/bandwidth before the rest) so a profile replay never retunes into
a filter or sample rate that can't hold it.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PropertySpec:
    name: str  # wire name, e.g. "device_vfo_frequency"
    field: str | None  # ReceiverState field name, None if not part of state
    kind: type  # int | float | bool | str
    read_only: bool = False
    stage: int = 0  # restore stage 2..7; 0 = not restorable
    tolerance: float = 0.0  # abs tolerance when confirming a set
    timeout_s: float = 1.5
    critical: bool = False


_ROWS: list[PropertySpec] = [
    # Restorable, staged (2..7), in restore order.
    PropertySpec("active_antenna", "antenna", str, stage=2, timeout_s=1.5, critical=True),
    PropertySpec(
        "device_sample_rate", "sample_rate_hz", int, stage=3, timeout_s=5.0, critical=True
    ),
    PropertySpec(
        "device_center_frequency", "center_hz", int, stage=4, timeout_s=5.0, critical=True
    ),
    PropertySpec("device_vfo_frequency", "vfo_hz", int, stage=5, timeout_s=1.5, critical=True),
    PropertySpec("demodulator", "demodulator", str, stage=6, timeout_s=1.5, critical=True),
    PropertySpec(
        "filter_bandwidth", "filter_bandwidth_hz", int, stage=6, timeout_s=1.5, critical=True
    ),
    PropertySpec("lna_state", "lna_state", int, stage=7, timeout_s=1.5),
    PropertySpec("squelch_enable", "squelch_enabled", bool, stage=7, timeout_s=1.5),
    PropertySpec(
        "squelch_threshold", "squelch_threshold", float, stage=7, tolerance=0.5, timeout_s=1.5
    ),
    PropertySpec("agc_enable", "audio_agc_enabled", bool, stage=7, timeout_s=1.5),
    PropertySpec(
        "agc_threshold", "audio_agc_threshold", float, stage=7, tolerance=0.5, timeout_s=1.5
    ),
    PropertySpec(
        "noise_reduction_enable", "noise_reduction_enabled", bool, stage=7, timeout_s=1.5
    ),
    PropertySpec(
        "noise_reduction_strength", "noise_reduction_strength", int, stage=7, timeout_s=1.5
    ),
    # Read-only, not restorable.
    PropertySpec("signal_power", None, float, read_only=True),
    PropertySpec("signal_snr", None, float, read_only=True),
    PropertySpec("overload", None, bool, read_only=True),
    PropertySpec("can_control", None, bool, read_only=True),
    PropertySpec("started", None, bool, read_only=True),
    PropertySpec("api_version", None, str, read_only=True),
    PropertySpec("valid_antennas", None, str, read_only=True),
    PropertySpec("demod_max_bandwidth", None, int, read_only=True),
    PropertySpec("lna_state_min", None, int, read_only=True),
    PropertySpec("lna_state_max", None, int, read_only=True),
]

REGISTRY: dict[str, PropertySpec] = {row.name: row for row in _ROWS}

FIELD_TO_PROP: dict[str, str] = {
    row.field: row.name for row in _ROWS if row.field is not None
}

RESTORABLE: list[PropertySpec] = sorted(
    (row for row in _ROWS if row.stage > 0), key=lambda row: row.stage
)

READ_ONLY_SEED: list[str] = [
    "api_version",
    "can_control",
    "valid_antennas",
    "signal_power",
    "signal_snr",
    "overload",
    "started",
]


def _decode_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1")
    return bool(raw)


def decode(name: str, raw: object) -> object:
    """Decode a wire value (str or number) to its Python type per the registry.

    Unknown properties pass through unchanged -- msdr should tolerate SDRconnect
    exposing properties this registry doesn't know about yet.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return raw
    if spec.kind is bool:
        return _decode_bool(raw)
    if spec.kind is int:
        # Values sometimes arrive as bare JSON numbers (e.g. 145800000.0)
        # despite the spec saying strings; float() handles both forms.
        return int(float(raw))
    if spec.kind is float:
        return float(raw)
    return str(raw)


def encode(name: str, value: object) -> str:
    """Encode a Python value to the wire string form. Every outgoing value is a string."""
    spec = REGISTRY.get(name)
    kind = spec.kind if spec is not None else None
    if kind is bool:
        return "true" if value else "false"
    if kind is int:
        return str(int(value))
    if kind is float:
        return repr(float(value))
    return str(value)
