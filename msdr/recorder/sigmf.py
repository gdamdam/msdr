"""SigMF 1.0 metadata for a merged run of IQ segments (iqscan reads this directly)."""

from datetime import UTC, datetime

SIGMF_VERSION = "1.0.0"
DATATYPE = "ci16_le"  # complex int16, little-endian -- matches the raw device IQ we store


def annotation(sample_start: int, label: str, comment: str | None = None) -> dict:
    a: dict = {"core:sample_start": int(sample_start), "core:label": label}
    if comment:
        a["core:comment"] = comment
    return a


def build_meta(
    *,
    sample_rate: int,
    center_hz: int,
    start_wall: float,
    annotations: list[dict],
    description: str = "msdr preroll recording",
) -> dict:
    start_dt = datetime.fromtimestamp(start_wall, tz=UTC)
    return {
        "global": {
            "core:datatype": DATATYPE,
            "core:sample_rate": sample_rate,
            "core:version": SIGMF_VERSION,
            "core:recorder": "msdr",
            "core:description": description,
        },
        "captures": [
            {
                "core:sample_start": 0,
                "core:frequency": center_hz,
                "core:datetime": start_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            }
        ],
        "annotations": annotations,
    }
