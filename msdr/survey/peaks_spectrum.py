"""SpectrumPeakTracker: tracks peaks across 512-bin spectrum frames.

Finds contiguous runs of bins above the per-frame floor, merges runs
separated by a gap that scales with run width (so a wide, modulated signal's
occasional dead bin doesn't split it), computes a level-weighted centroid,
and matches each frame's peaks to existing tracks primarily by bin-range
overlap (a wide signal's centroid can wander by more than a couple of bins
between frames even though its footprint still overlaps); a narrow-peak
fallback keeps the old nearest-centroid rule (+-1.5 bins) for width<=2 peaks
whose single-bin footprint may not overlap frame to frame. Tracks that
drift into substantial overlap are merged, so a wandering wideband carrier
reads as one steady track instead of flickering duplicates. Cheap: numpy
ops over 512 bins, once per frame, so it can run inline in the spectrum
pump alongside OccupancyMap.
"""

from dataclasses import dataclass, field

import numpy as np

_BINS = 512
_THRESHOLD_UNITS = 24
_DC_EXCLUDE_BINS = 2
_TTL_S = 120.0
_HISTORY_S = 600.0
_MAX_ON_RUNS = 200
_MATCH_RADIUS_BINS = 1.5  # fallback centroid-distance match, narrow peaks only
_GAP_BINS = 1  # minimum bins allowed between active runs before merging
_RANGE_EMA_ALPHA = 0.2  # slow contraction of a track's bin range; expansion is instant
_DEDUPE_OVERLAP_FRAC = 0.5  # merge tracks overlapping more than this fraction of the narrower


@dataclass
class TrackedPeak:
    """One tracked signal. `on_runs` holds *closed* contiguous active spans
    (start, end); while `active_now` is True the current span is still open
    and its start is `run_start` -- classify.activity_class folds that
    still-open span into duty/observed so a steady carrier (always matched,
    so its run never closes) isn't scored as 0% duty."""

    id: str
    bin_lo: int
    bin_hi: int
    centroid_bin: float
    level: int
    snr_units: float
    first_seen: float
    last_seen: float
    on_runs: list[tuple[float, float]] = field(default_factory=list)
    active_now: bool = True
    run_start: float = 0.0
    profile: list[int] = field(default_factory=list)  # per-bin levels of the last-matched frame
    profile_lo: int = 0  # bin index of profile[0] (bin_lo is smoothed, the profile is not)
    peak_bin: int | None = None  # strongest current bin: marker anchor on the trace


def _find_spans(active: np.ndarray) -> list[tuple[int, int]]:
    """Group indices of a boolean array into (lo, hi) runs, merging runs
    separated by a gap of up to max(_GAP_BINS, width_so_far // 4) inactive
    bins -- a wide, modulated signal can have a few dead bins mid-span
    without splitting into two peaks, while narrow peaks keep the tight
    1-bin tolerance."""
    idx = np.flatnonzero(active)
    if idx.size == 0:
        return []
    spans = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        width_so_far = prev - start + 1
        gap_allowed = max(_GAP_BINS, width_so_far // 4)
        if i - prev <= gap_allowed + 1:
            prev = i
        else:
            spans.append((start, prev))
            start = prev = i
    spans.append((start, prev))
    return spans


def _overlap(lo1: int, hi1: int, lo2: int, hi2: int) -> int:
    return max(0, min(hi1, hi2) - max(lo1, lo2) + 1)


def _merge_on_runs(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Union two on_run lists so a merge doesn't double-count overlapping
    or adjacent active spans."""
    merged: list[tuple[float, float]] = []
    for s, e in sorted(a + b):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged[-_MAX_ON_RUNS:]


class SpectrumPeakTracker:
    def __init__(
        self,
        bins: int = _BINS,
        threshold_units: int = _THRESHOLD_UNITS,
        dc_exclude_bins: int = _DC_EXCLUDE_BINS,
        ttl_s: float = _TTL_S,
        history_s: float = _HISTORY_S,
    ) -> None:
        self.bins = bins
        self.threshold_units = threshold_units
        self.dc_exclude_bins = dc_exclude_bins
        self.ttl_s = ttl_s
        # Track ids exempt from ttl_s (Survey pins recognised signals: a named
        # family, an RDS station, a confirmed decode) -- they only expire
        # after pinned_ttl_s, None meaning never.
        self.pinned: set[str] = set()
        self.pinned_ttl_s: float | None = None
        self.history_s = history_s
        self._tracks: dict[str, TrackedPeak] = {}
        self._seq = 0

    def update(self, frame: bytes, at: float, floor: int) -> list[TrackedPeak]:
        levels = np.frombuffer(frame, dtype=np.uint8).astype(np.int32)
        if levels.size != self.bins:
            return list(self._tracks.values())  # malformed frame -- ignore, same as OccupancyMap

        active = levels > (floor + self.threshold_units)
        if self.dc_exclude_bins >= 0:
            center = self.bins // 2
            lo_ex = max(0, center - self.dc_exclude_bins)
            hi_ex = min(self.bins, center + self.dc_exclude_bins + 1)
            active[lo_ex:hi_ex] = False
            # Width-scaled gap tolerance must not bridge the excluded bins.
            spans = _find_spans(active[:lo_ex])
            spans.extend((lo + hi_ex, hi + hi_ex) for lo, hi in _find_spans(active[hi_ex:]))
        else:
            spans = _find_spans(active)
        candidates = []
        for lo, hi in spans:
            seg = levels[lo : hi + 1]
            weights = np.clip(seg - floor, 0, None).astype(np.float64)
            total = float(weights.sum())
            if total > 0:
                positions = np.arange(lo, hi + 1, dtype=np.float64)
                centroid = float((positions * weights).sum() / total)
            else:
                centroid = (lo + hi) / 2.0
            candidates.append(
                {
                    "lo": lo,
                    "hi": hi,
                    "centroid": centroid,
                    "level": int(seg.max()),
                    "peak_bin": lo + int(np.argmax(seg)),
                    "snr": float(seg.max() - floor),
                    "profile": seg.tolist(),
                }
            )

        # Primary matching: bin-range overlap (or centroid falling inside the
        # track's range +-1), preferring the largest overlap -- a wide,
        # modulated signal's centroid can wander past the old fixed radius
        # while its footprint still overlaps the track's range.
        primary_pairs = []
        for ci, cand in enumerate(candidates):
            for tid, track in self._tracks.items():
                overlap = _overlap(cand["lo"], cand["hi"], track.bin_lo, track.bin_hi)
                centroid_in_range = (track.bin_lo - 1) <= cand["centroid"] <= (track.bin_hi + 1)
                if overlap > 0 or centroid_in_range:
                    primary_pairs.append((overlap, ci, tid))
        primary_pairs.sort(key=lambda p: -p[0])
        cand_to_track: dict[int, str] = {}
        used_tracks: set[str] = set()
        for _overlap_n, ci, tid in primary_pairs:
            if ci in cand_to_track or tid in used_tracks:
                continue
            cand_to_track[ci] = tid
            used_tracks.add(tid)

        # Fallback: narrow peaks (width <= 2 bins) may not overlap frame to
        # frame at all, so fall back to the old nearest-centroid rule.
        fallback_pairs = []
        for ci, cand in enumerate(candidates):
            if ci in cand_to_track or (cand["hi"] - cand["lo"] + 1) > 2:
                continue
            for tid, track in self._tracks.items():
                if tid in used_tracks:
                    continue
                d = abs(cand["centroid"] - track.centroid_bin)
                if d <= _MATCH_RADIUS_BINS:
                    fallback_pairs.append((d, ci, tid))
        fallback_pairs.sort(key=lambda p: p[0])
        for _d, ci, tid in fallback_pairs:
            if ci in cand_to_track or tid in used_tracks:
                continue
            cand_to_track[ci] = tid
            used_tracks.add(tid)

        seen_ids = set()
        for ci, cand in enumerate(candidates):
            tid = cand_to_track.get(ci)
            if tid is None:
                track = self._create_track(cand, at)
            else:
                track = self._tracks[tid]
                self._touch_track(track, cand, at)
            seen_ids.add(track.id)

        for tid, track in self._tracks.items():
            if tid not in seen_ids and track.active_now:
                self._close_run(track)

        self._dedupe_tracks()

        # Drop tracks unseen for ttl_s, then prune on_runs older than history_s
        # (a run counts as "old" once its end falls outside the history window).
        cutoff = at - self.history_s
        self._tracks = {
            tid: t
            for tid, t in self._tracks.items()
            if at - t.last_seen <= self.ttl_s
            or (
                tid in self.pinned
                and (self.pinned_ttl_s is None or at - t.last_seen <= self.pinned_ttl_s)
            )
        }
        for track in self._tracks.values():
            track.on_runs = [(s, e) for s, e in track.on_runs if e >= cutoff][-_MAX_ON_RUNS:]

        return list(self._tracks.values())

    def reset(self) -> None:
        self._tracks = {}
        self._seq = 0

    def _create_track(self, cand: dict, at: float) -> TrackedPeak:
        tid = f"t{self._seq}"
        self._seq += 1
        track = TrackedPeak(
            id=tid,
            bin_lo=cand["lo"],
            bin_hi=cand["hi"],
            centroid_bin=cand["centroid"],
            level=cand["level"],
            snr_units=cand["snr"],
            first_seen=at,
            last_seen=at,
            active_now=True,
            run_start=at,
            profile=cand["profile"],
            profile_lo=cand["lo"],
            peak_bin=cand["peak_bin"],
        )
        self._tracks[tid] = track
        return track

    def _touch_track(self, track: TrackedPeak, cand: dict, at: float) -> None:
        # Range expands instantly (max-hold) but only contracts via a slow
        # EMA, so a modulated signal's per-frame width jitter doesn't make
        # the track's range flicker and lose overlap with the next frame.
        track.bin_lo = (
            cand["lo"]
            if cand["lo"] <= track.bin_lo
            else round(track.bin_lo + _RANGE_EMA_ALPHA * (cand["lo"] - track.bin_lo))
        )
        track.bin_hi = (
            cand["hi"]
            if cand["hi"] >= track.bin_hi
            else round(track.bin_hi + _RANGE_EMA_ALPHA * (cand["hi"] - track.bin_hi))
        )
        track.centroid_bin = cand["centroid"]
        track.level = cand["level"]
        track.snr_units = cand["snr"]
        track.profile = cand["profile"]
        track.profile_lo = cand["lo"]
        track.peak_bin = cand["peak_bin"]
        track.last_seen = at
        if not track.active_now:
            track.active_now = True
            track.run_start = at

    def _close_run(self, track: TrackedPeak) -> None:
        track.on_runs.append((track.run_start, track.last_seen))
        track.on_runs = track.on_runs[-_MAX_ON_RUNS:]
        track.active_now = False

    def _dedupe_tracks(self) -> None:
        """Merge tracks whose bin ranges have drifted into substantial
        overlap (e.g. a wide signal briefly tracked as two ids): keep the
        older id, union the range, and union the on_run history."""
        ids = list(self._tracks.keys())
        for i, tid_a in enumerate(ids):
            if tid_a not in self._tracks:
                continue
            for tid_b in ids[i + 1 :]:
                if tid_b not in self._tracks:
                    continue
                track_a = self._tracks[tid_a]
                track_b = self._tracks[tid_b]
                # Smoothed historical ranges can encompass two currently
                # separate narrow carriers after a wide region splits. Merge
                # only if their latest measured profiles still overlap.
                lo_a, hi_a = track_a.profile_lo, track_a.profile_lo + len(track_a.profile) - 1
                lo_b, hi_b = track_b.profile_lo, track_b.profile_lo + len(track_b.profile) - 1
                narrower = min(hi_a - lo_a + 1, hi_b - lo_b + 1)
                overlap = _overlap(lo_a, hi_a, lo_b, hi_b)
                if narrower <= 0 or overlap <= narrower * _DEDUPE_OVERLAP_FRAC:
                    continue
                older, newer = (
                    (track_a, track_b)
                    if track_a.first_seen <= track_b.first_seen
                    else (track_b, track_a)
                )
                older.bin_lo = min(older.bin_lo, newer.bin_lo)
                older.bin_hi = max(older.bin_hi, newer.bin_hi)
                older.first_seen = min(older.first_seen, newer.first_seen)
                older.last_seen = max(older.last_seen, newer.last_seen)
                if newer.snr_units > older.snr_units:
                    older.level = newer.level
                    older.snr_units = newer.snr_units
                    older.centroid_bin = newer.centroid_bin
                    older.profile = newer.profile
                    older.profile_lo = newer.profile_lo
                    older.peak_bin = newer.peak_bin
                if newer.active_now and not older.active_now:
                    older.active_now = True
                    older.run_start = newer.run_start
                elif newer.active_now and older.active_now:
                    older.run_start = min(older.run_start, newer.run_start)
                older.on_runs = _merge_on_runs(older.on_runs, newer.on_runs)
                del self._tracks[newer.id]
                if newer.id == tid_a:
                    break  # tid_a was absorbed -- nothing left to compare it against
