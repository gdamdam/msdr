import asyncio
import json

import pytest

from msdr.providers.iqscan import IqScanProvider, IqScanRunner, ScanNotFound

EVENT_WITH_HINTS = {
    "id": 1,
    "kind": "transient",
    "start_s": 114.327552,
    "end_s": 116.49024,
    "duration_s": 2.162688,
    "bandwidth_hz": 1342.773,
    "contrast_db": 22.55,
    "frequency_hz": 137711415.367,
    "band_references": [{"name": "Ch.8 — OrbComm Ch.S-8 is 137.7125MHz"}],
    "known_signal_hints": [{"name": "SVOM — Mode V - Transmitter 1"}],
    "signal_analysis": {"protocol": "LRPT"},
}

EVENT_NO_HINTS = {
    "id": 2,
    "kind": "transient",
    "start_s": 63.504384,
    "end_s": 66.3552,
    "duration_s": 2.850816,
    "bandwidth_hz": 366.211,
    "contrast_db": 17.15,
    "frequency_hz": 138008656.578,
}


def _write_scan(root, name: str, *, events: list[dict], report: bool = False) -> None:
    scan_dir = root / name
    scan_dir.mkdir()
    doc = {
        "metadata": {
            "input": "/tmp/x.cs8",
            "format": "cs8",
            "sample_rate": 500000.0,
            "center_frequency_hz": 137900014.0,
            "duration_s": 187.84256,
        },
        "settings": {},
        "events": events,
    }
    (scan_dir / "events.json").write_text(json.dumps(doc))
    if report:
        (scan_dir / "report.html").write_text("<html></html>")


@pytest.fixture
def root(tmp_path):
    # Older result first, newer (with more events, no report) second -- tests
    # newest-first ordering and per-scan has_report independently.
    _write_scan(
        tmp_path, "scanA_20260101-120000-000000", events=[EVENT_WITH_HINTS], report=True
    )
    _write_scan(
        tmp_path,
        "scanB_20260102-130000-000000",
        events=[EVENT_NO_HINTS, EVENT_WITH_HINTS],
        report=False,
    )
    return tmp_path


def test_list_scans_newest_first_with_summary_fields(root):
    provider = IqScanProvider(str(root))
    scans = provider.list_scans()

    assert [s.id for s in scans] == [
        "scanB_20260102-130000-000000",
        "scanA_20260101-120000-000000",
    ]
    newest, oldest = scans
    assert newest.event_count == 2
    assert newest.has_report is False
    assert oldest.event_count == 1
    assert oldest.has_report is True
    assert newest.center_frequency_hz == 137900014
    assert newest.sample_rate == 500000.0
    assert newest.duration_s == 187.84256


def test_list_scans_ignores_dirs_without_events_json(root):
    (root / "not_a_scan").mkdir()
    provider = IqScanProvider(str(root))
    assert len(provider.list_scans()) == 2


def test_list_scans_on_missing_root_is_empty(tmp_path):
    provider = IqScanProvider(str(tmp_path / "does-not-exist"))
    assert provider.list_scans() == []


def test_events_parses_hints_and_protocol(root):
    provider = IqScanProvider(str(root))
    events = provider.events("scanA_20260101-120000-000000")

    assert len(events) == 1
    event = events[0]
    assert event.id == 1
    assert event.kind == "transient"
    assert event.frequency_hz == 137711415
    assert event.bandwidth_hz == 1342
    assert event.contrast_db == 22.55
    assert event.protocol == "LRPT"
    assert "Ch.8 — OrbComm Ch.S-8 is 137.7125MHz" in event.hints
    assert "SVOM — Mode V - Transmitter 1" in event.hints


def test_event_without_hints_or_protocol(root):
    provider = IqScanProvider(str(root))
    events = provider.events("scanB_20260102-130000-000000")
    no_hint_event = next(e for e in events if e.id == 2)

    assert no_hint_event.hints == []
    assert no_hint_event.protocol is None


def test_events_unknown_id_raises(root):
    provider = IqScanProvider(str(root))
    with pytest.raises(ScanNotFound):
        provider.events("does-not-exist")


def test_events_path_traversal_rejected(root):
    provider = IqScanProvider(str(root))
    with pytest.raises(ScanNotFound):
        provider.events("../etc")
    with pytest.raises(ScanNotFound):
        provider.events("scanA_20260101-120000-000000/../../etc")
    with pytest.raises(ScanNotFound):
        provider.events("scanA_20260101-120000-000000\\..\\etc")


def test_report_path_whitelist(root):
    provider = IqScanProvider(str(root))
    scan_id = "scanA_20260101-120000-000000"

    assert provider.report_path(scan_id, "report.html") is not None
    assert provider.report_path(scan_id, "events.json") is None  # not whitelisted
    assert provider.report_path(scan_id, "waterfall.png") is None  # whitelisted but absent
    assert provider.report_path("../etc", "report.html") is None  # traversal rejected
    assert provider.report_path("does-not-exist", "report.html") is None


# --- IqScanRunner --------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr

    def kill(self):
        pass

    async def wait(self):
        pass


def _install_fake_subprocess(
    monkeypatch, calls, *, make_scan_dir=None, returncode=0, stderr=b"", missing=False
):
    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        if missing:
            raise FileNotFoundError(args[0])
        if make_scan_dir is not None:
            make_scan_dir()
        return _FakeProc(returncode, stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


async def test_run_creates_scan_dir_and_job_is_done(tmp_path, monkeypatch):
    calls = []

    def make_scan_dir():
        scan_dir = tmp_path / "myfile_20260101-120000-000000"
        scan_dir.mkdir()
        (scan_dir / "events.json").write_text(json.dumps({"events": []}))

    _install_fake_subprocess(monkeypatch, calls, make_scan_dir=make_scan_dir)
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    job = runner.start(
        str(tmp_path / "input.iq"), sample_rate=2_000_000, center_hz=100_000_000, threshold=6.0
    )
    assert job.status == "queued"
    await runner._tasks[job.job_id]

    assert job.status == "done"
    assert job.scan_id == "myfile_20260101-120000-000000"
    assert job.error is None

    argv = calls[0]
    assert argv[0] == "iqscan"
    # Options first, then "--" guarding the positional path (a filename
    # starting with a dash must not be read as a flag).
    assert list(argv[-2:]) == ["--", str(tmp_path / "input.iq")]
    assert "--offline-references" in argv
    assert argv[argv.index("--output") + 1] == str(tmp_path)
    assert argv[argv.index("--sample-rate") + 1] == "2000000"
    assert argv[argv.index("--center-frequency") + 1] == "100000000"
    assert argv[argv.index("--threshold") + 1] == "6.0"


async def test_run_nonzero_exit_reports_stderr_tail(tmp_path, monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, returncode=1, stderr=b"boom: bad iq file")
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    job = runner.start(str(tmp_path / "input.iq"))
    await runner._tasks[job.job_id]

    assert job.status == "failed"
    assert job.scan_id is None
    assert "boom: bad iq file" in job.error


async def test_run_missing_binary_marks_job_failed(tmp_path, monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, missing=True)
    runner = IqScanRunner(bin="does-not-exist", scan_root=str(tmp_path))

    job = runner.start(str(tmp_path / "input.iq"))
    await runner._tasks[job.job_id]

    assert job.status == "failed"
    assert job.error == "iqscan not installed"


async def test_has_active_job_for_tracks_queued_and_running(tmp_path, monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_create_subprocess_exec(*args, **kwargs):
        started.set()
        await finish.wait()
        return _FakeProc(0, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    path = str(tmp_path / "input.iq")
    job = runner.start(path)
    await started.wait()
    assert runner.has_active_job_for(path) is True

    finish.set()
    await runner._tasks[job.job_id]
    assert runner.has_active_job_for(path) is False


async def test_jobs_newest_first_and_capped_at_twenty(tmp_path, monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls)
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    jobs = [runner.start(str(tmp_path / f"input{i}.iq")) for i in range(25)]
    for job in jobs:
        task = runner._tasks.get(job.job_id)
        if task is not None:
            await task

    listed = runner.jobs()
    assert len(listed) == 20
    assert listed[0].path == str(tmp_path / "input24.iq")  # newest first


async def test_publishes_status_changes(tmp_path, monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls)
    published = []

    async def on_status(job):
        published.append(job["status"])

    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path), on_status=on_status)
    job = runner.start(str(tmp_path / "input.iq"))
    await runner._tasks[job.job_id]

    assert published == ["running", "done"]


async def test_cancel_all_kills_running_subprocess(tmp_path, monkeypatch):
    started = asyncio.Event()
    killed = []

    class _HangingProc:
        returncode = None

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            killed.append(True)

        async def wait(self):
            pass

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    runner.start(str(tmp_path / "input.iq"))
    await started.wait()
    await runner.cancel_all()

    assert killed == [True]
    assert runner._tasks == {}


@pytest.mark.parametrize(
    ("analysis", "expected"),
    [
        (
            {"protocol": {"name": "AX.25", "status": "confirmed", "evidence": ["crc"]}},
            ("AX.25", "confirmed", ["crc"]),
        ),
        (
            {"protocol": {"name": None, "status": "unconfirmed", "evidence": []}},
            (None, None, []),
        ),
        ({"protocol": "LRPT"}, ("LRPT", None, [])),
    ],
)
def test_protocol_reads_iqscan_1_2_object_and_legacy_string(analysis, expected):
    # iqscan 1.2 turned signal_analysis.protocol into an object; a raw dict
    # reaching the str field used to raise ValidationError and 500 the scan.
    from msdr.providers.iqscan import _to_event

    event = _to_event(
        {
            "id": 1,
            "kind": "burst",
            "frequency_hz": 145_825_000,
            "bandwidth_hz": 12_000,
            "contrast_db": 10.0,
            "start_s": 0.0,
            "end_s": 1.0,
            "duration_s": 1.0,
            "signal_analysis": analysis,
        }
    )
    assert (event.protocol, event.protocol_status, event.protocol_evidence) == expected


def test_to_event_reads_channel_clip_basename():
    from msdr.providers.iqscan import _to_event

    event = _to_event(
        {
            "id": 3,
            "kind": "burst",
            "frequency_hz": 145_825_000,
            "bandwidth_hz": 12_000,
            "contrast_db": 10.0,
            "start_s": 0.0,
            "end_s": 1.0,
            "duration_s": 1.0,
            "channel_clip": "channels/event-03_8000SPS.cf32",
        }
    )
    assert event.channel_clip == "event-03_8000SPS.cf32"


def test_to_event_without_channel_clip_is_none():
    from msdr.providers.iqscan import _to_event

    event = _to_event(
        {
            "id": 4,
            "kind": "burst",
            "frequency_hz": 145_825_000,
            "bandwidth_hz": 12_000,
            "contrast_db": 10.0,
            "start_s": 0.0,
            "end_s": 1.0,
            "duration_s": 1.0,
        }
    )
    assert event.channel_clip is None


def test_clip_path_whitelist_and_traversal(root):
    provider = IqScanProvider(str(root))
    scan_id = "scanA_20260101-120000-000000"
    channels = root / scan_id / "channels"
    channels.mkdir()
    (channels / "event-01_8000SPS.cf32").write_bytes(b"\x00" * 8)

    assert provider.clip_path(scan_id, "event-01_8000SPS.cf32") is not None
    assert provider.clip_path(scan_id, "event-01_8000SPS.cf32.txt") is None  # not whitelisted
    assert provider.clip_path(scan_id, "../report.html") is None  # traversal rejected
    assert provider.clip_path("../etc", "event-01_8000SPS.cf32") is None
    assert provider.clip_path(scan_id, "event-99_8000SPS.cf32") is None  # absent


def test_redetect_dir_requires_cached_spectrum(root):
    provider = IqScanProvider(str(root))
    scan_id = "scanA_20260101-120000-000000"

    assert provider.redetect_dir(scan_id) is None  # no spectrum.npz yet
    (root / scan_id / "spectrum.npz").write_bytes(b"\x00")
    assert provider.redetect_dir(scan_id) == root / scan_id
    assert provider.redetect_dir("../etc") is None


async def test_run_channel_clips_format_redetect_argv(tmp_path, monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls)
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    job = runner.start(
        str(tmp_path / "input.cu8"), format="cu8", channel_clips=True
    )
    await runner._tasks[job.job_id]
    argv = calls[0]
    assert argv[argv.index("--format") + 1] == "cu8"
    assert argv[argv.index("--channel-clips") + 1] == "10"
    assert list(argv[-2:]) == ["--", str(tmp_path / "input.cu8")]


async def test_run_redetect_argv_has_no_file_positional(tmp_path, monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls)
    runner = IqScanRunner(bin="iqscan", scan_root=str(tmp_path))

    job = runner.start(redetect=str(tmp_path / "scanA"))
    await runner._tasks[job.job_id]
    argv = calls[0]
    assert argv[argv.index("--redetect") + 1] == str(tmp_path / "scanA")
    assert "--" not in argv
