"""Recordings browser (U1 plan, Stream A backend item 5): GET /api/recordings,
DELETE /api/recordings/{name}, POST /api/recordings/{name}/reveal, and POST
/api/recordings/{name}/analyze. Uses a FakeRecorder (tests/api/test_recorder.py,
gates _require_recorder_feature) with settings.recorder_root pointed at a
temp dir holding a hand-built synthetic SigMF recording -- the real
msdr.recorder.Recorder never runs in this file."""

import contextlib
import json
from pathlib import Path

import httpx
import pytest

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from tests.api.test_recorder import FakeRecorder


@contextlib.asynccontextmanager
async def running_app(tmp_path: Path, *, recorder=None):
    adapter = FakeAdapter()
    settings = Settings(
        dwell_seconds=0.05, db_path=":memory:", recorder_root=str(tmp_path)
    )
    db = Database(":memory:")
    recorder = recorder if recorder is not None else FakeRecorder()
    app = create_app(settings, adapter=adapter, db=db, recorder=recorder)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


def _write_synthetic_recording(root: Path, name: str = "2026-01-01_00-00-00_100000000Hz") -> Path:
    """A single-run recording: 10 complex int16 samples @ 1000 SPS, one gap
    annotation and one REC annotation, plus the recording.json index
    Recorder._finalize writes (see msdr/recorder/recorder.py)."""
    d = root / "recordings" / name
    d.mkdir(parents=True)
    stem = "run-01_1000SPS_100000000Hz"
    data = bytes(range(40))  # 10 samples * 4 bytes -- content doesn't matter here
    (d / f"{stem}.sigmf-data").write_bytes(data)
    meta = {
        "global": {"core:datatype": "ci16_le", "core:sample_rate": 1000, "core:version": "1.0.0"},
        "captures": [{"core:sample_start": 0, "core:frequency": 100_000_000}],
        "annotations": [
            {"core:sample_start": 0, "core:label": "REC", "core:comment": "recording started"},
            {"core:sample_start": 5, "core:label": "gap", "core:comment": "missing ~3 samples"},
        ],
    }
    (d / f"{stem}.sigmf-meta").write_text(json.dumps(meta))
    index = {
        "start_wall": 1_700_000_000.0,
        "end_wall": 1_700_000_010.0,
        "preroll_s": 300,
        "runs": [
            {
                "data": f"{stem}.sigmf-data",
                "meta": f"{stem}.sigmf-meta",
                "sample_rate": 1000,
                "center_hz": 100_000_000,
                "samples": 10,
                "start_wall": 1_700_000_000.0,
                "settings": {},
            }
        ],
    }
    (d / "recording.json").write_text(json.dumps(index))
    return d


# --- GET /api/recordings ----------------------------------------------------


async def test_recordings_list_empty_root(tmp_path):
    async with running_app(tmp_path) as (app, client):
        resp = await client.get("/api/recordings")
        assert resp.status_code == 200
        assert resp.json() == []


async def test_recordings_list_reads_synthetic_recording(tmp_path):
    _write_synthetic_recording(tmp_path)
    async with running_app(tmp_path) as (app, client):
        resp = await client.get("/api/recordings")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "2026-01-01_00-00-00_100000000Hz"
        assert row["center_hz"] == 100_000_000
        assert row["sample_rate"] == 1000
        assert row["duration_s"] == 10 / 1000
        assert row["size_bytes"] == 40
        assert row["gaps"] == 1
        assert row["created"] == 1_700_000_000.0
        # Review shell: reception time only when recording.json says so.
        assert row["capture_start"] == 1_700_000_000.0
        assert row["runs"] == 1
        assert row["path"].endswith(row["name"])


async def test_recordings_without_index_have_unknown_capture_start(tmp_path):
    # A directory mtime is never presented as the capture time:
    # without recording.json `capture_start` is null while `created` keeps
    # the mtime fallback for list ordering.
    _write_synthetic_recording(tmp_path)
    for index in (tmp_path / "recordings").rglob("recording.json"):
        index.unlink()
    async with running_app(tmp_path) as (app, client):
        rows = (await client.get("/api/recordings")).json()
        assert len(rows) == 1
        assert rows[0]["capture_start"] is None
        assert rows[0]["created"] is not None


async def test_recordings_list_survives_one_damaged_recording(tmp_path):
    # A structurally-invalid *.sigmf-meta (valid JSON, but not an object --
    # e.g. a truncated/corrupted write leaves `null`) used to crash
    # `meta.get(...)` inside the list comprehension and 500 the whole
    # endpoint, hiding the healthy recording too. One damaged entry must
    # become an error row instead, leaving the healthy one intact.
    _write_synthetic_recording(tmp_path, name="good")
    bad = tmp_path / "recordings" / "bad"
    bad.mkdir(parents=True)
    (bad / "x.sigmf-meta").write_text("null")
    (bad / "x.sigmf-data").write_bytes(bytes(40))
    async with running_app(tmp_path) as (app, client):
        resp = await client.get("/api/recordings")
        assert resp.status_code == 200
        rows = {r["name"]: r for r in resp.json()}
        assert set(rows) == {"good", "bad"}
        assert rows["good"]["error"] is None
        assert rows["good"]["center_hz"] == 100_000_000
        assert rows["bad"]["error"]
        assert "NoneType" in rows["bad"]["error"]
        assert rows["bad"]["center_hz"] is None
        assert rows["bad"]["path"].endswith("bad")


async def test_recordings_list_sums_durations_at_each_run_sample_rate(tmp_path):
    d = _write_synthetic_recording(tmp_path)
    stem = "run-02_2000SPS_100000000Hz"
    (d / f"{stem}.sigmf-data").write_bytes(bytes(80))  # 20 samples at 2000 SPS
    (d / f"{stem}.sigmf-meta").write_text(
        json.dumps(
            {
                "global": {"core:datatype": "ci16_le", "core:sample_rate": 2000},
                "captures": [{"core:sample_start": 0, "core:frequency": 100_000_000}],
                "annotations": [],
            }
        )
    )
    async with running_app(tmp_path) as (_app, client):
        row = (await client.get("/api/recordings")).json()[0]
        assert row["runs"] == 2
        assert row["sample_rate"] == 1000
        assert row["duration_s"] == pytest.approx(10 / 1000 + 20 / 2000)


# --- DELETE /api/recordings/{name} ------------------------------------------


async def test_recordings_delete_removes_directory(tmp_path):
    d = _write_synthetic_recording(tmp_path)
    async with running_app(tmp_path) as (app, client):
        resp = await client.delete(f"/api/recordings/{d.name}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        assert not d.exists()


async def test_recordings_delete_traversal_refused(tmp_path):
    _write_synthetic_recording(tmp_path)
    async with running_app(tmp_path) as (app, client):
        # ".." resolves outside the recordings root -- refused like any
        # other not-found name, not followed. Percent-encoded so httpx/ASGI
        # routing doesn't collapse the segment before it ever reaches the
        # route (a plain ".." in the URL gets normalized away first).
        resp = await client.delete("/api/recordings/%2e%2e")
        assert resp.status_code == 404
        # the synthetic recording must be untouched
        assert (tmp_path / "recordings" / "2026-01-01_00-00-00_100000000Hz").exists()


async def test_recordings_delete_root_refused(tmp_path):
    d = _write_synthetic_recording(tmp_path)
    async with running_app(tmp_path) as (_app, client):
        resp = await client.delete("/api/recordings/%2E")
        assert resp.status_code == 404
        assert d.exists()


@pytest.mark.parametrize("state", ["recording", "finalizing"])
async def test_recordings_delete_refused_while_recorder_writes(tmp_path, state):
    d = _write_synthetic_recording(tmp_path)
    recorder = FakeRecorder()
    async with running_app(tmp_path, recorder=recorder) as (_app, client):
        recorder.state = state
        resp = await client.delete(f"/api/recordings/{d.name}")
        assert resp.status_code == 409
        assert d.exists()
        recorder.state = "armed"
        resp = await client.delete(f"/api/recordings/{d.name}")
        assert resp.status_code == 200
        assert not d.exists()


async def test_recordings_delete_unknown_name_is_404(tmp_path):
    async with running_app(tmp_path) as (app, client):
        resp = await client.delete("/api/recordings/does-not-exist")
        assert resp.status_code == 404


# --- POST /api/recordings/{name}/reveal -------------------------------------


async def test_recordings_reveal_opens_the_recording_folder(tmp_path, monkeypatch):
    import msdr.api.services.recorder as recorder_module

    d = _write_synthetic_recording(tmp_path)
    calls = []
    monkeypatch.setattr(recorder_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        recorder_module.subprocess, "run", lambda args, **kw: calls.append(args)
    )
    async with running_app(tmp_path) as (app, client):
        resp = await client.post(f"/api/recordings/{d.name}/reveal")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "error": None}
        assert calls == [["open", str(d)]]


async def test_recordings_reveal_reports_subprocess_error(tmp_path, monkeypatch):
    import subprocess

    import msdr.api.services.recorder as recorder_module

    d = _write_synthetic_recording(tmp_path)
    monkeypatch.setattr(recorder_module.sys, "platform", "darwin")

    def _boom(args, **kw):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(recorder_module.subprocess, "run", _boom)
    async with running_app(tmp_path) as (app, client):
        resp = await client.post(f"/api/recordings/{d.name}/reveal")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]


async def test_recordings_reveal_non_macos_returns_path(tmp_path, monkeypatch):
    import msdr.api.services.recorder as recorder_module

    d = _write_synthetic_recording(tmp_path)
    monkeypatch.setattr(recorder_module.sys, "platform", "linux")
    async with running_app(tmp_path) as (app, client):
        resp = await client.post(f"/api/recordings/{d.name}/reveal")
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "error": None, "path": str(d)}


# --- POST /api/recordings/{name}/analyze ------------------------------------


class _FakeJob:
    def __init__(self, job_id: str, status: str) -> None:
        self.job_id = job_id
        self.status = status


class FakeIqScanRunner:
    def __init__(self) -> None:
        self.start_calls: list[tuple] = []

    def has_active_job_for(self, path: str) -> bool:
        return False

    async def cancel_all(self) -> None:
        pass

    def start(self, path, *, sample_rate=None, center_hz=None, threshold=None, format=None,
              channel_clips=False):
        self.start_calls.append((path, sample_rate, center_hz))
        return _FakeJob("job-1", "queued")


async def test_recordings_analyze_delegates_to_iqscan_runner(tmp_path):
    d = _write_synthetic_recording(tmp_path)
    fake_runner = FakeIqScanRunner()
    async with running_app(tmp_path) as (app, client):
        app.state.service.iqscan_runner = fake_runner
        resp = await client.post(f"/api/recordings/{d.name}/analyze")
        assert resp.status_code == 200
        assert resp.json() == {"job_id": "job-1", "status": "queued"}
        assert len(fake_runner.start_calls) == 1
        path, sample_rate, center_hz = fake_runner.start_calls[0]
        assert path == str(d / "run-01_1000SPS_100000000Hz.sigmf-data")
        assert sample_rate == 1000
        assert center_hz == 100_000_000


async def test_recordings_unreadable_root_is_503_not_empty(tmp_path):
    # An external volume this process may not read must surface as
    # "unavailable", never as a quietly empty list.
    root = tmp_path / "recordings"
    root.mkdir()
    root.chmod(0o000)
    try:
        async with running_app(tmp_path) as (app, client):
            resp = await client.get("/api/recordings")
            assert resp.status_code == 503
            assert "not readable" in resp.json()["detail"]
    finally:
        root.chmod(0o755)


async def test_recordings_list_skips_wifi_packet_sessions(tmp_path):
    # <recordings_root>/wifi holds Wi-Fi packet-capture sessions, shown in
    # Review's Wi-Fi sessions panel; it must not appear as a damaged IQ recording.
    _write_synthetic_recording(tmp_path, name="good")
    session = tmp_path / "recordings" / "wifi" / "1790963148-77ef086d"
    session.mkdir(parents=True)
    (session / "part-000.pcap").write_bytes(bytes(24))
    async with running_app(tmp_path) as (app, client):
        names = {r["name"] for r in (await client.get("/api/recordings")).json()}
        assert names == {"good"}
