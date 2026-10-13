"""Frequency-adaptive LNA: pick an LNA state from settings.lna_auto's band
table on tune, and decide when a sustained overload should add attenuation.

Pure helpers -- AppService does the I/O (see tune_frequency/tune_signal and
_lna_overload_watch)."""

from msdr.settings import LnaAutoSettings


def band_index(hz: float | None, config: LnaAutoSettings) -> int | None:
    if hz is None:
        return None
    for i, band in enumerate(config.bands):
        if band.up_to_hz is None or hz < band.up_to_hz:
            return i
    return None


def auto_lna_state(
    target_hz: float,
    current_hz: float | None,
    config: LnaAutoSettings,
    lna_max: int | None = None,
    lna_min: int | None = None,
) -> int | None:
    """The band's LNA state when tuning target_hz crosses into a different
    band than current_hz (or current is unknown), else None -- retuning
    within a band keeps whatever LNA the operator (or the backoff) set."""
    if not config.enabled:
        return None
    target = band_index(target_hz, config)
    if target is None or target == band_index(current_hz, config):
        return None
    state = config.bands[target].lna_state
    if lna_max is not None:
        state = min(state, lna_max)
    if lna_min is not None:
        state = max(state, lna_min)
    return state


def backoff_state(
    current: int | None,
    baseline: int | None,
    config: LnaAutoSettings,
    lna_max: int | None = None,
) -> int | None:
    """Next LNA state for a sustained overload, or None when backoff is off,
    the device is already at max attenuation, or this band has used up
    max_backoff_steps over its baseline (the state the band table applied,
    or the state when the overload began if none)."""
    if not (config.enabled and config.overload_backoff) or current is None:
        return None
    ceiling = (baseline if baseline is not None else current) + config.max_backoff_steps
    if lna_max is not None:
        ceiling = min(ceiling, lna_max)
    return current + 1 if current < ceiling else None
