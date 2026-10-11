"""Safe device policy for inference embedded in the live server."""

import copy
import sys

LIVE_CPU_REASON = (
    "Live ML uses CPU on macOS: Metal inference has caused a native process abort. "
    "Offline CLI benchmarks can still use MPS."
)


def live_settings(settings):
    """Copy settings; never mutate the persisted device preference.

    Python cannot catch Metal SIGABRT. Until inference is process-isolated,
    keep both live models off MPS, including an explicit MPS preference.
    """
    if sys.platform == "darwin" and settings.device in {"auto", "mps"}:
        effective = copy.copy(settings)
        effective.device = "cpu"
        return effective, LIVE_CPU_REASON
    return settings, None
