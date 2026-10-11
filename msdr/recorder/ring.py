"""On-disk segment ring: writes IQ frames into rotating .cs16 files.

A single SegmentRing is not concurrency-safe by itself -- Recorder is the
only caller and serializes all access to a given ring behind its own lock,
so no locking is needed in here.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# int16 I + int16 Q per complex sample.
_BYTES_PER_SAMPLE = 4


@dataclass
class SegmentMeta:
    path: Path  # data file (*.cs16)
    meta_path: Path  # sidecar (*.json)
    start_wall: float
    sample_rate: int
    center_hz: int
    settings: dict = field(default_factory=dict)
    samples: int = 0
    gaps: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def duration_s(self) -> float:
        return self.samples / self.sample_rate if self.sample_rate else 0.0

    def to_json(self) -> dict:
        return {
            "start_wall": self.start_wall,
            "sample_rate": self.sample_rate,
            "center_hz": self.center_hz,
            "settings": self.settings,
            "samples": self.samples,
            "gaps": self.gaps,
            "events": self.events,
        }

    @classmethod
    def from_json(cls, data_path: Path, meta_path: Path, d: dict) -> SegmentMeta:
        return cls(
            path=data_path,
            meta_path=meta_path,
            start_wall=d["start_wall"],
            sample_rate=d["sample_rate"],
            center_hz=d["center_hz"],
            settings=d.get("settings", {}),
            samples=d.get("samples", 0),
            gaps=d.get("gaps", []),
            events=d.get("events", []),
        )


class SegmentRing:
    def __init__(self, directory: Path):
        self.directory = directory
        self._current: SegmentMeta | None = None
        self._fh = None

    @property
    def current(self) -> SegmentMeta | None:
        return self._current

    def open_segment(
        self, sample_rate: int, center_hz: int, settings: dict, now: float
    ) -> SegmentMeta:
        # `now` comes from Recorder's injectable clock rather than time.time()
        # directly, so tests can drive segment rotation deterministically.
        self.directory.mkdir(parents=True, exist_ok=True)
        ns = int(now * 1e9)
        segment_id = f"seg-{ns}_{sample_rate}SPS_{center_hz}Hz_{uuid.uuid4().hex[:8]}"
        data_path = self.directory / f"{segment_id}.cs16"
        meta_path = self.directory / f"{segment_id}.json"
        current = SegmentMeta(
            path=data_path,
            meta_path=meta_path,
            start_wall=now,
            sample_rate=sample_rate,
            center_hz=center_hz,
            settings=dict(settings),
        )
        # Persist enough receiver metadata before creating the IQ file so a
        # process crash can still identify an unclosed segment.
        meta_path.write_text(json.dumps(current.to_json()))
        try:
            self._fh = open(data_path, "xb")
        except Exception:
            meta_path.unlink(missing_ok=True)
            raise
        self._current = current
        return current

    def write(self, data: bytes) -> None:
        """Append raw IQ bytes to the current segment (blocking I/O -- call via to_thread)."""
        assert self._fh is not None
        self._fh.write(data)
        self._current.samples += len(data) // _BYTES_PER_SAMPLE

    def record_gap(self, missing_estimate: int) -> None:
        if self._current is not None and missing_estimate > 0:
            self._current.gaps.append(
                {"sample": self._current.samples, "missing_estimate": missing_estimate}
            )

    def record_event(self, prop: str, value: object) -> None:
        if self._current is not None:
            self._current.events.append(
                {"sample": self._current.samples, "property": prop, "value": value}
            )

    def close_segment(self) -> SegmentMeta | None:
        """Close and flush the sidecar; drop empty segments (nothing was written)."""
        cur = self._current
        if cur is None:
            return None
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._current = None
        if cur.samples == 0:
            cur.path.unlink(missing_ok=True)
            cur.meta_path.unlink(missing_ok=True)
            return None
        cur.meta_path.write_text(json.dumps(cur.to_json()))
        return cur

    def closed_segments(self) -> list[SegmentMeta]:
        """Closed segments in this directory, oldest first (filenames sort by time)."""
        metas = []
        known_paths = set()
        if not self.directory.exists():
            return metas
        for meta_path in sorted(self.directory.glob("seg-*.json")):
            data_path = meta_path.with_suffix(".cs16")
            if not data_path.exists():
                continue
            if self._current is not None and data_path == self._current.path:
                continue
            try:
                d = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            file_size = data_path.stat().st_size
            complete_bytes = file_size - file_size % _BYTES_PER_SAMPLE
            d["samples"] = complete_bytes // _BYTES_PER_SAMPLE
            if complete_bytes:
                metas.append(SegmentMeta.from_json(data_path, meta_path, d))
                known_paths.add(data_path)

        # Newer segment names carry the centre and rate, allowing recovery if
        # the sidecar itself was lost or corrupted during a power failure.
        for data_path in sorted(self.directory.glob("seg-*.cs16")):
            meta_path = data_path.with_suffix(".json")
            if self._current is not None and data_path == self._current.path:
                continue
            if data_path in known_paths:
                continue
            match = re.fullmatch(
                r"seg-(\d+)_(\d+)SPS_(\d+)Hz_[0-9a-f]{8}\.cs16", data_path.name
            )
            if match is None:
                continue
            ns, rate, center = map(int, match.groups())
            file_size = data_path.stat().st_size
            complete_bytes = file_size - file_size % _BYTES_PER_SAMPLE
            segment = SegmentMeta(
                path=data_path,
                meta_path=meta_path,
                start_wall=ns / 1e9,
                sample_rate=rate,
                center_hz=center,
                samples=complete_bytes // _BYTES_PER_SAMPLE,
            )
            if segment.samples:
                metas.append(segment)
        metas.sort(key=lambda m: m.start_wall)
        return metas

    def recover_open_segments(self) -> list[SegmentMeta]:
        """Normalize crash-left segments before a repair/merge operation."""
        segments = self.closed_segments()
        for segment in segments:
            size = segment.path.stat().st_size
            complete_size = size - size % _BYTES_PER_SAMPLE
            if complete_size != size:
                with segment.path.open("r+b") as stream:
                    stream.truncate(complete_size)
            segment.samples = complete_size // _BYTES_PER_SAMPLE
            segment.meta_path.write_text(json.dumps(segment.to_json()))
        return segments

    def buffered_s(self) -> float:
        # Include the open segment: it is part of what REC would promote.
        current = self._current.duration_s() if self._current is not None else 0.0
        return sum(m.duration_s() for m in self.closed_segments()) + current

    def evict(self, keep_s: float) -> None:
        """Delete oldest closed segments while the rest still cover keep_s.

        Stopping at "total <= keep_s" would leave up to one segment less than
        the promised pre-roll; only drop a segment the remainder can spare."""
        metas = self.closed_segments()  # oldest first
        total = sum(m.duration_s() for m in metas)
        for m in metas:
            if total - m.duration_s() < keep_s:
                break
            total -= m.duration_s()
            m.path.unlink(missing_ok=True)
            m.meta_path.unlink(missing_ok=True)

    def clear(self) -> None:
        """Delete all segment files -- used at startup, the buffer is disposable."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._current = None
        if self.directory.exists():
            for p in self.directory.glob("seg-*"):
                p.unlink(missing_ok=True)
