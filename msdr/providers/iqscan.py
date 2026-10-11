"""iqscan result provider: reads finished offline IQ-scan result folders from disk.

Read-only, no web/network imports (F1, mirrors bandplan.py) -- iqscan itself
runs out-of-process and writes `events.json` per scan; this just parses what's
already on disk. `ScanNotFound` is translated to a 404 by msdr/api/app.py.

`IqScanRunner` (below) is the write side: it launches the user's `iqscan`
binary as a background subprocess against an IQ file and tracks the resulting
job, mirroring providers/nextpass.py's subprocess pattern.
"""

import asyncio
import contextlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from msdr.library.models import now_iso

# Whitelisted files servable via GET /api/scans/{id}/report/{name} -- an
# open-ended name would let a caller read anything the process can see.
REPORT_NAMES = {"report.html", "waterfall.png", "spectrum-context.png"}

# Channel-clip filenames iq_export.py writes under <scan>/channels/ (see
# --channel-clips): f"event-{id:02d}_{rate:g}SPS.cf32". Names are matched
# against this pattern rather than a fixed set (each scan's clips differ),
# so GET /api/scans/{id}/clips/{name} still can't be pointed outside that
# one directory.
_CLIP_NAME_RE = re.compile(r"^event-\d+_[0-9.]+SPS\.cf32$")

# Trailing `_<YYYYMMDD-HHMMSS-ffffff>` timestamp iqscan appends to its result
# directory name (the rest of the name is the source IQ file's stem).
_DIR_TIMESTAMP_RE = re.compile(r"_(\d{8}-\d{6}-\d+)$")


class ScanNotFound(Exception):
    """Raised by events() for an unknown scan id, or one with path separators."""


class ScanEvent(BaseModel):
    id: int
    kind: str
    frequency_hz: int
    bandwidth_hz: int
    contrast_db: float
    start_s: float
    end_s: float
    duration_s: float
    hints: list[str] = []
    protocol: str | None = None
    # iqscan >= 1.2 reports unconfirmed|candidate|confirmed; None for older
    # scans, whose bare-string protocol carried no confidence.
    protocol_status: str | None = None
    # iqscan >= 1.2's signal_analysis.protocol.evidence -- short strings
    # backing protocol/protocol_status (e.g. a decoded frame count); [] when
    # there's no protocol dict (older scans) or no evidence listed.
    protocol_evidence: list[str] = []
    # channels/event-<id>_<rate>SPS.cf32 when this scan was run with
    # --channel-clips and produced one for this event (iq_export.py); None
    # otherwise. Served via GET /api/scans/{id}/clips/{name}.
    channel_clip: str | None = None


def _protocol(event: dict) -> tuple[str | None, str | None, list[str]]:
    """(name, status, evidence) from signal_analysis.protocol, which iqscan
    1.2 turned from a bare string into {"name", "status", "evidence"}; both
    shapes are read so scans made by either version keep loading."""
    raw = (event.get("signal_analysis") or {}).get("protocol")
    if isinstance(raw, dict):
        name = raw.get("name")
        evidence = [str(e) for e in (raw.get("evidence") or []) if e]
        return (str(name) if name else None), (raw.get("status") if name else None), evidence
    return (str(raw) if raw else None), None, []


class ScanSummary(BaseModel):
    id: str
    path: str
    created: str
    center_frequency_hz: int
    sample_rate: float
    duration_s: float
    event_count: int
    has_report: bool


def _is_safe_id(scan_id: str) -> bool:
    return "/" not in scan_id and "\\" not in scan_id and ".." not in scan_id


def _created_at(scan_dir: Path) -> str:
    match = _DIR_TIMESTAMP_RE.search(scan_dir.name)
    if match is not None:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S-%f").isoformat()
        except ValueError:
            pass  # fall through to mtime below
    return datetime.fromtimestamp(scan_dir.stat().st_mtime).isoformat()


def _summary(scan_dir: Path, doc: dict) -> ScanSummary:
    metadata = doc.get("metadata", {})
    return ScanSummary(
        id=scan_dir.name,
        path=str(scan_dir),
        created=_created_at(scan_dir),
        center_frequency_hz=int(metadata.get("center_frequency_hz") or 0),
        sample_rate=metadata.get("sample_rate") or 0.0,
        duration_s=metadata.get("duration_s") or 0.0,
        event_count=len(doc.get("events", [])),
        has_report=(scan_dir / "report.html").is_file(),
    )


def _event_hints(event: dict) -> list[str]:
    # band_references are community band-plan overlaps; known_signal_hints
    # are SatNOGS transmitter matches -- both are name-only overlap hints,
    # not a decoded identification (see iqscan's own disclaimer).
    refs = event.get("band_references") or []
    hints = event.get("known_signal_hints") or []
    return [r["name"] for r in refs if r.get("name")] + [h["name"] for h in hints if h.get("name")]


def _to_event(event: dict) -> ScanEvent:
    protocol, protocol_status, protocol_evidence = _protocol(event)
    channel_clip = event.get("channel_clip")
    return ScanEvent(
        id=event["id"],
        kind=event["kind"],
        frequency_hz=int(event["frequency_hz"]),
        bandwidth_hz=int(event["bandwidth_hz"]),
        contrast_db=event["contrast_db"],
        start_s=event["start_s"],
        end_s=event["end_s"],
        duration_s=event["duration_s"],
        hints=_event_hints(event),
        protocol=protocol,
        protocol_status=protocol_status,
        protocol_evidence=protocol_evidence,
        # Stored as just the basename (iq_export.py writes "channels/<name>")
        # -- the route only ever serves out of that one subdirectory.
        channel_clip=Path(channel_clip).name if channel_clip else None,
    )


class IqScanProvider:
    name = "iqscan"

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser()

    def _scan_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return [d for d in self.root.iterdir() if d.is_dir() and (d / "events.json").is_file()]

    def list_scans(self) -> list[ScanSummary]:
        summaries = []
        for scan_dir in self._scan_dirs():
            try:
                doc = json.loads((scan_dir / "events.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue  # skip a corrupt/partial result dir rather than 500 the whole list
            summaries.append(_summary(scan_dir, doc))
        summaries.sort(key=lambda s: s.created, reverse=True)
        return summaries

    def events(self, scan_id: str) -> list[ScanEvent]:
        if not _is_safe_id(scan_id):
            raise ScanNotFound(scan_id)
        events_path = self.root / scan_id / "events.json"
        if not events_path.is_file():
            raise ScanNotFound(scan_id)
        # A half-written or corrupt events.json is treated like a missing scan,
        # matching list_scans(), instead of surfacing as a 500.
        try:
            doc = json.loads(events_path.read_text())
            return [_to_event(e) for e in doc.get("events", [])]
        except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValidationError) as exc:
            raise ScanNotFound(scan_id) from exc

    def report_path(self, scan_id: str, name: str) -> Path | None:
        if not _is_safe_id(scan_id) or name not in REPORT_NAMES:
            return None
        path = self.root / scan_id / name
        return path if path.is_file() else None

    def redetect_dir(self, scan_id: str) -> Path | None:
        """A previous scan directory usable with `iqscan --redetect` -- that
        needs a cached spectrum.npz (from a run made with --save-spectrum),
        not just any scan id (see run_scan)."""
        if not _is_safe_id(scan_id):
            return None
        path = self.root / scan_id
        return path if (path / "spectrum.npz").is_file() else None

    def clip_path(self, scan_id: str, name: str) -> Path | None:
        """GET /api/scans/{id}/clips/{name} -- name must match _CLIP_NAME_RE
        (see its comment), the same "whitelist, not just path-cleaning" shape
        as report_path above."""
        if not _is_safe_id(scan_id) or not _CLIP_NAME_RE.match(name):
            return None
        path = self.root / scan_id / "channels" / name
        return path if path.is_file() else None


# --- IqScanRunner: run `iqscan` on an IQ file and track jobs --------------

_JOB_TIMEOUT_S = 30 * 60
_JOB_STDERR_TAIL_CHARS = 500
_MAX_JOBS = 20
# --channel-clips takes an event count, not a bool; msdr's job form only
# offers on/off, so "on" asks for as many clips as iqscan's own --clips
# default (events beyond this just don't get one).
_CHANNEL_CLIPS_COUNT = 10
# mtime slop for "created after started_at" -- tolerates filesystem mtime
# granularity/clock skew between capturing start_epoch and the subprocess
# actually creating its output directory.
_NEW_SCAN_SLOP_S = 1.0

JobStatus = Literal["queued", "running", "done", "failed"]


class ScanJob(BaseModel):
    job_id: str
    path: str
    status: JobStatus
    started_at: str
    finished_at: str | None = None
    scan_id: str | None = None
    error: str | None = None


class IqScanRunner:
    """Launches `iqscan` on an IQ file as a background asyncio task and
    tracks the resulting job. Jobs are kept in memory only (last `_MAX_JOBS`);
    `on_status`, if given, is awaited with the job's dict on each status
    change (queued -> running -> done/failed)."""

    def __init__(
        self,
        bin: str = "iqscan",
        scan_root: str = "",
        *,
        on_status: Callable[[dict], Awaitable[None]] | None = None,
        timeout_s: float = _JOB_TIMEOUT_S,
    ) -> None:
        self.bin = bin
        self.scan_root = scan_root
        self.timeout_s = timeout_s
        self._on_status = on_status
        # Insertion-ordered: oldest first, so eviction pops from the front
        # and jobs() reverses for newest-first.
        self._jobs: dict[str, ScanJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def jobs(self) -> list[ScanJob]:
        return list(self._jobs.values())[::-1]

    def has_active_job_for(self, path: str) -> bool:
        return any(
            j.path == path and j.status in ("queued", "running") for j in self._jobs.values()
        )

    def start(
        self,
        path: str | None = None,
        *,
        sample_rate: int | None = None,
        center_hz: int | None = None,
        threshold: float | None = None,
        format: str | None = None,
        channel_clips: bool = False,
        redetect: str | None = None,
    ) -> ScanJob:
        """`path` xor `redetect` (a scan-id directory under scan_root to
        rerun detection on -- see AppService.run_scan, which resolves and
        validates it); iqscan itself rejects giving both."""
        identity = path if path is not None else f"redetect:{redetect}"
        job = ScanJob(
            job_id=str(uuid.uuid4()), path=identity, status="queued", started_at=now_iso()
        )
        self._jobs[job.job_id] = job
        self._evict_old()
        task = asyncio.create_task(
            self._run(job, path, sample_rate, center_hz, threshold, format, channel_clips, redetect)
        )
        self._tasks[job.job_id] = task
        return job

    def _evict_old(self) -> None:
        while len(self._jobs) > _MAX_JOBS:
            oldest_id = next(iter(self._jobs))
            self._jobs.pop(oldest_id)
            self._tasks.pop(oldest_id, None)

    async def cancel_all(self) -> None:
        """Cancel every still-running job task. Called from AppService.stop()."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _run(
        self,
        job: ScanJob,
        path: str | None,
        sample_rate: int | None,
        center_hz: int | None,
        threshold: float | None,
        format: str | None,
        channel_clips: bool,
        redetect: str | None,
    ) -> None:
        job.status = "running"
        await self._notify(job)

        argv = [self.bin, "--offline-references", "--output", self.scan_root]
        if sample_rate is not None:
            argv += ["--sample-rate", str(sample_rate)]
        if center_hz is not None:
            argv += ["--center-frequency", str(center_hz)]
        if threshold is not None:
            argv += ["--threshold", str(threshold)]
        if format is not None:
            argv += ["--format", format]
        if channel_clips:
            argv += ["--channel-clips", str(_CHANNEL_CLIPS_COUNT)]
        if redetect is not None:
            # Reruns detection from a previous scan's cached spectrum.npz --
            # mutually exclusive with a fresh recording (iqscan itself
            # rejects both), so no trailing file positional here.
            argv += ["--redetect", redetect]
        else:
            # Options first, then "--": a recording whose name starts with a
            # dash would otherwise be parsed as a flag. The path is a
            # positional, so everything after "--" is taken literally by
            # iqscan's argparse.
            argv += ["--", path]

        # Captured before launching so it's never later than the moment the
        # subprocess -- and hence any result directory it creates -- exists.
        start_epoch = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            await self._fail(job, "iqscan not installed")
            return
        except OSError as exc:
            # e.g. iqscan_bin points at a non-executable file
            await self._fail(job, f"cannot run {self.bin}: {exc}")
            return

        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            await self._fail(job, f"{self.bin} timed out after {self.timeout_s}s")
            return
        except asyncio.CancelledError:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            raise

        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-_JOB_STDERR_TAIL_CHARS:]
            await self._fail(job, tail or f"{self.bin} exited {proc.returncode}")
            return

        job.scan_id = self._find_new_scan(start_epoch)
        job.status = "done"
        job.finished_at = now_iso()
        await self._notify(job)

    async def _fail(self, job: ScanJob, error: str) -> None:
        job.status = "failed"
        job.error = error
        job.finished_at = now_iso()
        await self._notify(job)

    async def _notify(self, job: ScanJob) -> None:
        if self._on_status is not None:
            await self._on_status(job.model_dump(mode="json"))

    def _find_new_scan(self, after_epoch: float) -> str | None:
        """Newest directory under the scan root created after `after_epoch`
        that contains events.json, or None if there isn't one."""
        root = Path(self.scan_root).expanduser()
        if not root.is_dir():
            return None
        threshold = after_epoch - _NEW_SCAN_SLOP_S
        best_name: str | None = None
        best_mtime = threshold
        for scan_dir in root.iterdir():
            if not scan_dir.is_dir() or not (scan_dir / "events.json").is_file():
                continue
            mtime = scan_dir.stat().st_mtime
            if mtime >= best_mtime:
                best_mtime = mtime
                best_name = scan_dir.name
        return best_name
