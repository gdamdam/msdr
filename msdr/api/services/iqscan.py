"""Offline `iqscan` jobs on IQ files.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import os
from pathlib import Path

from fastapi import HTTPException


class IqScanServiceMixin:
    # --- iqscan (run `iqscan` on an IQ file, track jobs) --------------------

    def run_scan(
        self,
        path: str | None = None,
        *,
        sample_rate: int | None = None,
        center_hz: int | None = None,
        threshold: float | None = None,
        format: str | None = None,
        channel_clips: bool = False,
        redetect: str | None = None,
    ) -> dict:
        if not self.settings.features.scans:
            raise HTTPException(404, "feature disabled: scans")
        if bool(path) == bool(redetect):
            raise HTTPException(400, "give exactly one of path or redetect")
        if redetect is not None:
            # --redetect reruns detection from a previous scan's cached
            # spectrum.npz -- no ownership check needed, it's already one of
            # our own scan-root directories, just path-safety-checked.
            redetect_dir = self.iqscan_provider.redetect_dir(redetect)
            if redetect_dir is None:
                raise HTTPException(400, f"no cached spectrum to redetect from: {redetect!r}")
            job = self.iqscan_runner.start(
                redetect=str(redetect_dir),
                sample_rate=sample_rate,
                center_hz=center_hz,
                threshold=threshold,
                format=format,
                channel_clips=channel_clips,
            )
            return {"job_id": job.job_id, "status": job.status}
        p = Path(path).expanduser()
        # Same ownership check as catalog_import_file, but every failure here
        # is a 400 per the /api/scans/run contract (no separate 403).
        if not p.exists() or not p.is_file():
            raise HTTPException(400, f"file not found: {path!r}")
        if p.stat().st_uid != os.getuid():
            raise HTTPException(400, "file not owned by current user")
        if self.iqscan_runner.has_active_job_for(str(p)):
            raise HTTPException(409, f"scan already queued or running for: {path!r}")
        job = self.iqscan_runner.start(
            str(p),
            sample_rate=sample_rate,
            center_hz=center_hz,
            threshold=threshold,
            format=format,
            channel_clips=channel_clips,
        )
        return {"job_id": job.job_id, "status": job.status}

    def scan_jobs(self) -> dict:
        if not self.settings.features.scans:
            raise HTTPException(404, "feature disabled: scans")
        return {"jobs": [j.model_dump(mode="json") for j in self.iqscan_runner.jobs()]}
