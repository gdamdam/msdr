"""A recording as an ordered list of runs on one elapsed-time axis.

A run is a stretch of samples at one centre/rate (msdr's recorder starts a
new run on every retune). The timeline maps file-relative elapsed seconds
to ``(run_index, local_sample)`` and back. Sources, in order of preference:

- ``recording.json`` in a recording directory (``runs[]``: data, meta,
  sample_rate, center_hz, samples, start_wall -- Recorder._finalize);
- the directory's sorted ``*.sigmf-meta`` files, one run each;
- a single SigMF file, or an SDRconnect IQ WAV (one run, start_wall None:
  the WAV name carries the centre frequency but no trustworthy time).

Gaps within and between runs are only ever *known* from the recording itself
(SigMF ``gap`` annotations, or an explicit ``gap_before_samples`` in
recording.json). An unmarked gap between runs is unknown (``None``) and
contributes 0 s to the elapsed axis -- it is never invented from wall-clock
differences. Stored sample indices remain run-local and exclude missing data.
"""

import json
import math
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_CENTER_IN_NAME = re.compile(r"_(\d+)HZ", re.IGNORECASE)
_GAP_COMMENT = re.compile(r"missing ~?(\d+) samples")
_BYTES_PER_SAMPLE = 4  # ci16_le: int16 I + int16 Q


class RecordingError(ValueError):
    """The recording can't be read (unknown format, no centre frequency...)."""


@dataclass(frozen=True)
class TimelineRun:
    index: int
    path: Path  # the sample data file
    sample_rate_hz: int
    center_hz: int
    samples: int  # complex samples
    start_wall: float | None  # reception time of sample 0, if trustworthy
    # Samples missing before this run (previous run's rate); None = unknown.
    gap_before_samples: int | None
    data_offset: int = 0  # byte offset of sample 0 in `path` (WAV header)
    # (first stored sample after the gap, missing samples), in run-local indices.
    interior_gaps: tuple[tuple[int, int], ...] = ()
    meta_path: Path | None = None

    @property
    def gap_unknown(self) -> bool:
        return self.gap_before_samples is None

    @property
    def duration_s(self) -> float:
        return (self.samples + sum(n for _, n in self.interior_gaps)) / self.sample_rate_hz

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "path": str(self.path),
            "sample_rate_hz": self.sample_rate_hz,
            "center_hz": self.center_hz,
            "samples": self.samples,
            "duration_s": self.duration_s,
            "start_wall": self.start_wall,
            "gap_before_samples": self.gap_before_samples,
            "gap_unknown": self.gap_unknown,
            "interior_gaps": [
                {"sample_start": s, "missing_samples": n} for s, n in self.interior_gaps
            ],
        }


class RecordingTimeline:
    def __init__(self, runs: list[TimelineRun]) -> None:
        if not runs:
            raise RecordingError("recording has no runs")
        self.runs = runs
        # Elapsed second at which each run's sample 0 plays. A known gap is
        # measured in the previous run's rate; an unknown one adds nothing.
        self._starts: list[float] = []
        t = 0.0
        for i, run in enumerate(runs):
            if i and run.gap_before_samples:
                t += run.gap_before_samples / runs[i - 1].sample_rate_hz
            self._starts.append(t)
            t += run.duration_s
        self.total_elapsed_s = t
        self.total_samples = sum(r.samples for r in runs)

    def __len__(self) -> int:
        return len(self.runs)

    def run_start_s(self, run_index: int) -> float:
        return self._starts[run_index]

    def elapsed_s(self, run_index: int, local_sample: int) -> float:
        run = self.runs[run_index]
        missing = sum(n for s, n in run.interior_gaps if s <= local_sample)
        return self._starts[run_index] + (local_sample + missing) / run.sample_rate_hz

    def local_sample_at(self, run_index: int, elapsed_s: float) -> int:
        """Stored sample at elapsed time, including a run's end position.

        A position inside an annotated gap maps forward to its first stored
        sample. Missing samples never consume indices in the data file.
        """
        run = self.runs[run_index]
        ticks = max(0.0, (float(elapsed_s) - self._starts[run_index]) * run.sample_rate_hz)
        missing = 0
        for sample, count in run.interior_gaps:
            if ticks >= sample + missing - 1e-6 and ticks < sample + missing + count - 1e-6:
                return sample
            if ticks < sample + missing + count - 1e-6:
                break
            missing += count
        return min(run.samples, max(0, math.floor(ticks - missing + 1e-6)))

    def locate(self, elapsed_s: float) -> tuple[int, int]:
        """(run_index, local_sample) playing at `elapsed_s`, clamped to the
        recording: a position inside a gap maps to the start of the run after
        it, anything past the end to the last sample."""
        playable = [i for i, r in enumerate(self.runs) if r.samples > 0]
        if not playable:
            return 0, 0
        e = max(0.0, float(elapsed_s))
        last = playable[-1]
        if e >= self._starts[last] + self.runs[last].duration_s:
            return last, self.runs[last].samples - 1
        chosen = playable[0]
        for i in playable:
            if self._starts[i] <= e + 1e-12:
                chosen = i
            else:
                break
        run = self.runs[chosen]
        local = self.local_sample_at(chosen, e)
        if local >= run.samples:  # in the gap after `chosen`: next run's start
            later = [i for i in playable if i > chosen]
            return (later[0], 0) if later else (chosen, run.samples - 1)
        return chosen, max(0, local)

    def as_dicts(self) -> list[dict]:
        out = []
        for run in self.runs:
            d = run.as_dict()
            d["start_s"] = self._starts[run.index]
            out.append(d)
        return out

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_path(cls, path: str | Path) -> "RecordingTimeline":
        path = Path(path)
        if not path.exists():
            raise RecordingError(f"no such file: {path}")
        if path.is_dir():
            index = path / "recording.json"
            if index.exists():
                return cls._from_index(path, json.loads(index.read_text()))
            metas = sorted(path.glob("*.sigmf-meta"))
            if not metas:
                raise RecordingError(f"no SigMF segments in {path}")
            return cls._from_metas(metas)
        if path.suffix in (".sigmf-meta", ".sigmf-data"):
            return cls._from_metas([path.with_suffix(".sigmf-meta")])
        if path.suffix.lower() == ".wav":
            return cls([_wav_run(path)])
        raise RecordingError(f"unsupported recording: {path.name} (want .wav or SigMF)")

    @classmethod
    def _from_index(cls, root: Path, index: dict) -> "RecordingTimeline":
        entries = index.get("runs") or []
        if not entries:
            raise RecordingError(f"recording.json lists no runs: {root}")
        metas = []
        for entry in entries:
            meta_path = root / entry["meta"] if entry.get("meta") else None
            meta = _read_meta(meta_path) if meta_path and meta_path.exists() else None
            metas.append(meta)
        runs = []
        for i, entry in enumerate(entries):
            meta = metas[i]
            meta_path = root / entry["meta"] if entry.get("meta") else None
            data_path = root / entry["data"]
            on_disk = data_path.stat().st_size // _BYTES_PER_SAMPLE
            # Trust the index, but never past what's actually on disk.
            samples = min(int(entry.get("samples", on_disk)), on_disk)
            if "gap_before_samples" in entry:
                gap = entry["gap_before_samples"]
            elif i == 0:
                gap = 0
            else:
                prev = runs[-1]
                gap = _boundary_gap(metas[i - 1], prev.samples, metas[i])
            start_wall = entry.get("start_wall")
            runs.append(
                TimelineRun(
                    index=i,
                    path=data_path,
                    sample_rate_hz=int(entry["sample_rate"]),
                    center_hz=int(entry["center_hz"]),
                    samples=int(samples),
                    start_wall=float(start_wall) if start_wall is not None else None,
                    gap_before_samples=None if gap is None else int(gap),
                    interior_gaps=_interior_gaps(meta, samples),
                    meta_path=meta_path if meta is not None else None,
                )
            )
        return cls(runs)

    @classmethod
    def _from_metas(cls, meta_paths: list[Path]) -> "RecordingTimeline":
        runs: list[TimelineRun] = []
        prev_meta = None
        for i, meta_path in enumerate(meta_paths):
            meta = _read_meta(meta_path)
            glob = meta.get("global", {})
            if glob.get("core:datatype") != "ci16_le":
                raise RecordingError(f"unsupported SigMF datatype: {glob.get('core:datatype')}")
            capture = (meta.get("captures") or [{}])[0]
            center = int(capture.get("core:frequency") or 0)
            if not center:
                raise RecordingError(f"SigMF without core:frequency: {meta_path}")
            data_path = meta_path.with_suffix(".sigmf-data")
            samples = data_path.stat().st_size // _BYTES_PER_SAMPLE
            gap = 0 if i == 0 else _boundary_gap(prev_meta, runs[-1].samples, meta)
            runs.append(
                TimelineRun(
                    index=i,
                    path=data_path,
                    sample_rate_hz=int(glob["core:sample_rate"]),
                    center_hz=center,
                    samples=samples,
                    start_wall=_parse_datetime(capture.get("core:datetime")),
                    gap_before_samples=gap,
                    interior_gaps=_interior_gaps(meta, samples),
                    meta_path=meta_path,
                )
            )
            prev_meta = meta
        return cls(runs)


def _read_meta(path: Path) -> dict:
    return json.loads(path.read_text())


def _gap_annotations(meta: dict | None) -> list[tuple[int, int]]:
    """(sample_start, missing) for every `gap` annotation with a count."""
    out = []
    for a in (meta or {}).get("annotations", []):
        if a.get("core:label") != "gap":
            continue
        match = _GAP_COMMENT.search(a.get("core:comment") or "")
        if match:
            out.append((int(a.get("core:sample_start", 0)), int(match.group(1))))
    return out


def _boundary_gap(prev_meta: dict | None, prev_samples: int, meta: dict | None) -> int | None:
    """Samples missing between two runs, from the annotations that touch the
    boundary (end of the previous run, sample 0 of this one); None when the
    recording says nothing about it."""
    found = [n for s, n in _gap_annotations(prev_meta) if s >= prev_samples]
    found += [n for s, n in _gap_annotations(meta) if s == 0]
    return sum(found) if found else None


def _interior_gaps(meta: dict | None, samples: int) -> tuple[tuple[int, int], ...]:
    """Recorder gap markers inside stored data, coalesced by sample index."""
    by_sample: dict[int, int] = {}
    for sample, missing in _gap_annotations(meta):
        if 0 < sample < samples and missing > 0:
            by_sample[sample] = by_sample.get(sample, 0) + missing
    return tuple(sorted(by_sample.items()))


def _parse_datetime(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _wav_run(path: Path) -> TimelineRun:
    with open(path, "rb") as fh:
        head = fh.read(4096)
    if head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
        raise RecordingError(f"not a WAV file: {path.name}")
    pos, fmt, data_off, data_len, ds64_len = 12, None, None, None, None
    while pos + 8 <= len(head):
        cid, size = head[pos : pos + 4], struct.unpack("<I", head[pos + 4 : pos + 8])[0]
        body = pos + 8
        if cid == b"ds64":  # RF64: the real data size (the 32-bit field is 0xFFFFFFFF)
            ds64_len = struct.unpack("<Q", head[body + 8 : body + 16])[0]
        elif cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", head[body : body + 16])
        elif cid == b"data":
            data_off, data_len = body, size
            break
        pos = body + size + (size & 1)
    if fmt is None or data_off is None:
        raise RecordingError(f"WAV without fmt/data chunk: {path.name}")
    _tag, channels, rate, _byte_rate, _align, bits = fmt
    if channels != 2 or bits != 16:
        raise RecordingError(f"want 16-bit stereo I/Q, got {channels} ch x {bits} bit")
    if ds64_len is not None:
        data_len = ds64_len
    # A recording cut short (or >4 GB plain RIFF) has a bogus size field:
    # trust what's actually on disk.
    data_len = min(data_len, path.stat().st_size - data_off)
    match = _CENTER_IN_NAME.search(path.name)
    if match is None:
        raise RecordingError(f"no centre frequency in the name (..._<Hz>HZ.wav): {path.name}")
    return TimelineRun(
        index=0,
        path=path,
        sample_rate_hz=int(rate),
        center_hz=int(match.group(1)),
        samples=data_len // _BYTES_PER_SAMPLE,
        start_wall=None,
        gap_before_samples=0,
        data_offset=data_off,
    )
