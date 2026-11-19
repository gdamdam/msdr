"""Frame-sync search for narrowband digital voice: which system a 2400/4800 Bd
FSK signal is, from the fixed sync pattern each one puts in its frames.

A matching symbol rate cannot tell DMR from P25 from NXDN (see
`survey.families`); a sync pattern that repeats at the system's frame period
can. This is evidence of the *protocol only* -- nothing is decoded, no
identity, colour code, talkgroup or voice is read, and an encrypted call
looks the same as a clear one.

Method: FM-discriminate the channel, take one soft level per symbol at each
candidate symbol rate and timing phase, slice to four levels (+-1, +-3) and
compare every position with each pattern, both polarities. A hit alone is
never believed: random data matches a short pattern often, and a two-level
signal sliced to four levels sits on the outer levels, where the outer-only
patterns (DMR, P25, M17, dPMR) live. So a system is reported only when hits
repeat at its frame period, and for those outer-only patterns only when the
symbols between the hits really use four levels.

Sync words are the published ones (ETSI TS 102 361-1, TIA-102.BAAA, NXDN
TS 1-A, the System Fusion and M17 specifications, ETSI TS 102 658, JARL
D-STAR). Validated on synthetic signals only (tests/survey/test_framesync.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# Dibit -> 4FSK level, common to every system here: 01 +3, 00 +1, 10 -1, 11 -3.
_DIBIT_LEVEL = {0b01: 3, 0b00: 1, 0b10: -1, 0b11: -3}

_MIN_SPS = 4.0  # fewer samples per symbol than this and the timing search is too coarse
_MIN_SYMBOLS = 64
# Syncs shorter than this many symbols match random data too often to allow
# any symbol error, and need more repeats before they count.
_SHORT_SYNC = 16
_LONG_MIN_HITS = 2
_SHORT_MIN_HITS = 4
# A short sync must fill at least this share of the frame slots it spans:
# scattered chance matches that happen to share a frame phase do not.
_SHORT_MIN_DENSITY = 0.5
# Share of inner-level symbols a real four-level stream shows between hits
# (random data: one half). A two-level signal stays well under this.
_MIN_INNER_FRACTION = 0.25
# Envelope below this share of the capture's strong level is "no carrier":
# discriminator output there is noise several times the real deviation.
_ACTIVE_ENVELOPE = 0.25


def _levels(value: int, bits: int) -> np.ndarray:
    return np.array(
        [_DIBIT_LEVEL[(value >> (bits - 2 - 2 * i)) & 3] for i in range(bits // 2)],
        dtype=np.int8,
    )


def _signs(text: str) -> np.ndarray:
    """A two-level pattern in on-air order, '1' = positive deviation."""
    return np.array([1 if ch == "1" else -1 for ch in text], dtype=np.int8)


@dataclass(frozen=True)
class _Pattern:
    protocol: str
    decoder_id: str  # msdr.decoder.registry id a hit suggests
    name: str
    levels: np.ndarray
    period: int  # symbols between possible repeats of this sync
    two_level: bool = False  # compare signs only (GMSK)
    four_level_payload: bool = True  # gate on inner levels between hits

    @property
    def short(self) -> bool:
        return len(self.levels) < _SHORT_SYNC

    @property
    def outer_only(self) -> bool:
        return not self.two_level and bool(np.all(np.abs(self.levels) == 3))


def _p(protocol, decoder_id, name, value, bits, period) -> _Pattern:
    return _Pattern(protocol, decoder_id, name, _levels(value, bits), period)


# Periods are in symbols. DMR: one 30 ms burst. P25: every data unit is a
# whole number of 7.5 ms microslots. dPMR FS2 is in every second 80 ms frame.
_BY_BAUD: dict[int, tuple[_Pattern, ...]] = {
    4800: (
        _p("DMR", "dmr", "BS voice sync", 0x755FD7DF75F7, 48, 144),
        _p("DMR", "dmr", "BS data sync", 0xDFF57D75DF5D, 48, 144),
        _p("DMR", "dmr", "MS voice sync", 0x7F7D5DD57DFD, 48, 144),
        _p("DMR", "dmr", "MS data sync", 0xD5D7F77FD757, 48, 144),
        _p("P25 Phase 1", "p25", "frame sync", 0x5575F5FF77FF, 48, 36),
        _p("NXDN96", "nxdn96", "frame sync word", 0xCDF59, 20, 192),
        _p("System Fusion", "ysf", "frame sync", 0xD471C9634D, 40, 480),
        _p("M17", "m17", "stream sync", 0xFF5D, 16, 192),
        _p("M17", "m17", "packet sync", 0x75FF, 16, 192),
        _Pattern(
            "D-STAR",
            "dstar",
            "voice data sync",
            _signs("313131313133131113313111"),
            21 * 96,
            two_level=True,
            four_level_payload=False,
        ),
    ),
    2400: (
        _p("NXDN48", "nxdn", "frame sync word", 0xCDF59, 20, 192),
        _p("dPMR", "dpmr", "frame sync 2", 0x5FF77D, 24, 384),
    ),
}


@dataclass(frozen=True)
class FrameSync:
    """One system found. `hits` syncs were seen at the frame period, the worst
    with `symbol_errors` wrong symbols; `inverted` = the discriminator polarity
    is the opposite of the standard's (some receive chains flip it)."""

    protocol: str
    decoder_id: str
    sync: str
    symbol_rate_hz: int
    hits: int
    symbol_errors: int
    inverted: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _decide(sym: np.ndarray) -> np.ndarray | None:
    """Soft symbols -> hard levels in {-3,-1,1,3}; 0 where there was no carrier."""
    valid = np.isfinite(sym)
    if int(valid.sum()) < _MIN_SYMBOLS:
        return None
    # 5th/95th percentiles are the outer levels of any FSK stream and, unlike
    # the mean, do not move with a tuning offset's DC or an uneven symbol mix.
    lo, hi = np.percentile(sym[valid], [5.0, 95.0])
    half = (hi - lo) / 2.0
    if half <= 0:
        return None
    norm = (np.where(valid, sym, 0.0) - (hi + lo) / 2.0) * (3.0 / half)
    hard = np.where(norm >= 2, 3, np.where(norm >= 0, 1, np.where(norm >= -2, -1, -3)))
    hard[~valid] = 0
    return hard.astype(np.int8)


def _chain(positions: np.ndarray, pattern: _Pattern) -> np.ndarray:
    """The largest set of hits sharing one frame phase (+-1 symbol), one per
    frame slot; empty when it does not meet the pattern's repeat rule."""
    none = positions[:0]
    need = _SHORT_MIN_HITS if pattern.short else _LONG_MIN_HITS
    if len(positions) < need:
        return none
    residues = positions % pattern.period
    best = none
    for residue in np.unique(residues):
        distance = np.abs(residues - residue)
        members = positions[np.minimum(distance, pattern.period - distance) <= 1]
        if len(members) > len(best):
            best = members
    slots = np.rint((best - best[0]) / pattern.period).astype(int)
    best = best[np.unique(slots, return_index=True)[1]]
    if len(best) < need:
        return none
    if pattern.short and len(best) < _SHORT_MIN_DENSITY * (slots.max() + 1):
        return none
    return best


def _search(hard: np.ndarray, pattern: _Pattern) -> tuple[int, int, bool] | None:
    """(hits, worst symbol errors, inverted) for the better polarity, or None."""
    length = len(pattern.levels)
    if len(hard) < length:
        return None
    windows = sliding_window_view(np.sign(hard) if pattern.two_level else hard, length)
    tolerance = 0 if pattern.short else 1
    found = None
    for polarity in (1, -1):
        errors = (windows != polarity * pattern.levels).sum(axis=1)
        chain = _chain(np.nonzero(errors <= tolerance)[0], pattern)
        if not len(chain):
            continue
        if pattern.outer_only and pattern.four_level_payload:
            # Judge the symbols between the syncs, not the syncs themselves
            # (all outer by construction).
            payload = np.ones(chain[-1] + length - chain[0], dtype=bool)
            for hit in chain - chain[0]:
                payload[hit : hit + length] = False
            span = hard[chain[0] : chain[-1] + length][payload]
            if not len(span) or np.mean(np.abs(span) == 1) < _MIN_INNER_FRACTION:
                continue
        if found is None or len(chain) > found[0]:
            found = (len(chain), int(errors[chain].max()), polarity < 0)
    return found


def detect_frame_sync_disc(disc: np.ndarray, rate: float) -> list[FrameSync]:
    """Search FM discriminator output (any scale; NaN = no carrier) sampled at
    `rate` Hz. Returns the systems found, strongest evidence first; empty when
    none repeats. Needs about a second of signal for the sparse syncs (DMR
    voice every 360 ms, D-STAR every 420 ms)."""
    # Ranked by (evidence, standard polarity): DMR's data syncs are its voice
    # syncs inverted, so on a tie the reading that needs no inversion wins.
    best: dict[str, tuple[tuple[int, bool], FrameSync]] = {}
    for baud, patterns in _BY_BAUD.items():
        sps = rate / baud
        if sps < _MIN_SPS:
            continue
        # Half a symbol: enough averaging to steady the level, short enough
        # that the neighbouring symbols' shaped edges stay out of it.
        width = max(1, int(round(sps / 2)))
        smooth = np.convolve(disc, np.full(width, 1.0 / width), mode="same")
        count = int((len(smooth) - sps) // sps)
        if count < _MIN_SYMBOLS:
            continue
        base = np.arange(count) * sps
        for phase in range(int(sps)):
            hard = _decide(smooth[np.floor(base + phase + 0.5).astype(int)])
            if hard is None:
                continue
            for pattern in patterns:
                found = _search(hard, pattern)
                if found is None:
                    continue
                hits, errors, inverted = found
                score = (hits * len(pattern.levels), not inverted)
                if score > best.get(pattern.decoder_id, ((0, False), None))[0]:
                    best[pattern.decoder_id] = (
                        score,
                        FrameSync(
                            pattern.protocol,
                            pattern.decoder_id,
                            pattern.name,
                            baud,
                            hits,
                            errors,
                            inverted,
                        ),
                    )
    return [item for _, item in sorted(best.values(), key=lambda entry: entry[0], reverse=True)]


def detect_frame_sync(iq: np.ndarray, rate: float) -> list[FrameSync]:
    """As `detect_frame_sync_disc`, from the channel's complex baseband IQ
    (the signal near 0 Hz, a channel filter already applied)."""
    iq = np.nan_to_num(np.asarray(iq), nan=0.0, posinf=0.0, neginf=0.0)
    if len(iq) < 2:
        return []
    envelope = np.abs(iq)
    strong = float(np.percentile(envelope, 95.0))
    if strong <= 0:
        return []
    active = envelope > _ACTIVE_ENVELOPE * strong
    disc = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float64)
    disc[~(active[1:] & active[:-1])] = np.nan
    return detect_frame_sync_disc(disc, rate)
