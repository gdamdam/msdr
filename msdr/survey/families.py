"""Symbol rate + modulation -> protocol family hints (S3). Table straight
from the design doc/plan: known baud rates map to the protocols that use
them (never the frequency/allocation); `suggested_decoders` are the
decoder ids S5's "-> DECODE" handoff will start. 4800 baud 4-level FSK is
DMR/P25/NXDN voice (S6's dsd-fme decoders).

`family_for` itself carries no confidence: the plan puts that on the caller
("confidence from line strength x modulation confidence"), since neither
number is available from (modulation_kind, symbol_rate_hz, bw_class,
occ_bw_hz) alone -- the analyzer multiplies them in once it has both.
"""

import math

# Wide signals are never analysed for modulation/symbol rate at all (S2 label
# only, see Analyzer) -- this only documents that a family lookup for one is
# always a no-op, matching family_for's bw_class parameter.
_WIDE_BW_CLASSES = ("wfm", "wide_digital")

_BAUD_TOLERANCE = 0.03  # matches snap_symbol_rate's 3% window


def _close(rate: float, candidate: float) -> bool:
    return math.isclose(rate, candidate, rel_tol=_BAUD_TOLERANCE)


def _row(name: str, decoders: list[str], alternatives: list[str], next_action: str = "") -> dict:
    if not next_action:
        next_action = "decode " + " or ".join(decoders) if decoders else "listen; no tested decoder"
    return {
        "name": name,
        "suggested_decoders": decoders,
        "alternatives": alternatives,
        "next_action": next_action,
    }


KEYED_CARRIER = "keyed carrier (unknown code)"


def _family_for(
    modulation_kind: str,
    symbol_rate_hz: float | None,
    bw_class: str,
    occ_bw_hz: float,
    keying: dict | None = None,
) -> dict | None:
    if bw_class in _WIDE_BW_CLASSES:
        return None

    if modulation_kind == "carrier":
        return _row(
            "carrier / beacon",
            [],
            ["keyed carrier between elements"],
            "listen; a bare carrier carries no decodable identity",
        )

    if modulation_kind == "fm_fsk" and symbol_rate_hz is not None:
        if _close(symbol_rate_hz, 1200.0):
            return _row("POCSAG1200 / AX.25 AFSK", ["pocsag", "aprs"], ["other 1200 Bd FSK"])
        if _close(symbol_rate_hz, 2400.0):
            return _row("POCSAG2400", ["pocsag"], ["other 2400 Bd FSK"])
        if _close(symbol_rate_hz, 1600.0) or _close(symbol_rate_hz, 3200.0):
            return _row("FLEX", ["flex"], ["other 1600/3200 Bd FSK"])
        if _close(symbol_rate_hz, 4800.0):
            return _row("DMR / P25 / NXDN", ["dmr", "p25", "nxdn"], ["other 4800 Bd (4-level) FSK"])
        if _close(symbol_rate_hz, 9600.0):
            # The rate + 2-level FSK shape fit AIS, G3RUH packet and a C4FM
            # symbol clock alike; only a CRC-valid AIS frame (with an MMSI)
            # names AIS -- never the frequency/allocation.
            return _row(
                "GMSK/FSK 9600 (AIS or packet)",
                ["ais", "packet9600"],
                ["AX.25 9600 (G3RUH)", "P25 C4FM (4800 sym/s ×2)"],
                "decode ais or packet9600; AIS identity requires a CRC-valid frame with an MMSI",
            )
        if _close(symbol_rate_hz, 45.45):
            return _row("RTTY", ["rtty"], ["other 45 Bd FSK"])
        if _close(symbol_rate_hz, 50.0) or _close(symbol_rate_hz, 100.0):
            return _row("RTTY / NAVTEX", ["rtty"], ["other 50/100 Bd FSK"])
        return None

    if modulation_kind == "ook":
        # CW only when the keying is slow *and* the on-durations look like
        # dots and dashes (features.keying_timing); any other keyed carrier
        # stays a keyed carrier -- presence, not a code.
        unit_s = (keying or {}).get("unit_s")
        slow = (symbol_rate_hz is not None and symbol_rate_hz < 100.0) or (
            symbol_rate_hz is None and unit_s is not None and 0.02 <= unit_s <= 0.3
        )
        if slow and keying is not None and keying.get("fits_cw") is True:
            return _row("CW", ["cw"], [KEYED_CARRIER])
        return _row(
            KEYED_CARRIER,
            [],
            ["CW (dot/dash timing not confirmed)"] if slow else [],
            "listen; on/off keying without confirmed Morse timing",
        )

    return None


def family_for(
    modulation_kind: str,
    symbol_rate_hz: float | None,
    bw_class: str,
    occ_bw_hz: float,
    available_ids: set[str] | None = None,
    *,
    keying: dict | None = None,
) -> dict | None:
    """As `_family_for`, plus an optional `available_ids` filter: when given,
    `suggested_decoders` is narrowed to ids present in that set (e.g. the
    registry's currently-available decoders) -- default is no filtering, so
    existing callers are unaffected. Every family dict carries `name`,
    `suggested_decoders`, `alternatives` (other explanations the same
    evidence fits) and `next_action` (what would confirm it). `keying` is
    features.keying_timing()'s result for OOK signals (CW needs it)."""
    family = _family_for(modulation_kind, symbol_rate_hz, bw_class, occ_bw_hz, keying)
    if family is None or available_ids is None:
        return family
    return {
        **family,
        "suggested_decoders": [d for d in family["suggested_decoders"] if d in available_ids],
    }
