"""Review (S3 replay contract): GET/POST /api/review*, bookmarks, and the
/ws/audio transport.

Driven against a *real* SDRconnectAdapter talking to a FakeSDRconnect (not the
FakeAdapter test double used elsewhere) -- that's the only way `set_count()`
means anything: it proves review tuning/listening/seeking/bookmarking never
sends a single set_property/apply_device_profile to the radio, because that
whole path is real, not stubbed out.
"""

import asyncio
import json
import struct
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from msdr.api.app import create_app
from msdr.api.service import AppService
from msdr.api.services import review as review_module
from msdr.radio.fake_server import FakeSDRconnect
from msdr.radio.sdrconnect import SDRconnectAdapter
from msdr.settings import DecoderSettings, Features, Settings
from msdr.storage.db import Database

# This environment's starlette/anyio pairing emits import-time
# DeprecationWarnings unrelated to this test -- see tests/api/test_app.py's
# identical guard around the same import.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient  # noqa: E402

# A clean decimation ratio to 48 kS/s (the default VirtualReceiver audio_rate)
# and comfortably inside the "<=250 kS/s" ChannelExtractor bucket (S3 contract
# Sec2) -- avoids the 1:1 edge case a bare 48 kS/s recording would hit.
_SAMPLE_RATE_HZ = 240_000
_DURATION_S = 2.0
_CENTER_HZ = 100_000_000
_BANDWIDTH_HZ = 12_500


class _FakeServerThread:
    """Runs FakeSDRconnect on its own asyncio loop/thread so a synchronous
    `TestClient(app)` -- which drives the ASGI app (and the real
    SDRconnectAdapter inside it) on *its own* background loop -- can still
    reach it over a real loopback socket. Same idea as tests/e2e/conftest.py's
    `live_app`, minus uvicorn/the browser: nothing here needs a real HTTP
    server in front of the ASGI app itself."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.fake: FakeSDRconnect | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("fake SDRconnect server did not start in time")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        finally:
            self.loop.close()

    async def _main(self) -> None:
        self.fake = FakeSDRconnect(port=0)
        await self.fake.start()
        self._stop_event = asyncio.Event()
        self._ready.set()
        await self._stop_event.wait()
        await self.fake.stop()

    def stop(self) -> None:
        assert self._stop_event is not None
        self.loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=5)


def _write_synthetic_recording(root: Path, name: str = "review-test") -> Path:
    """A single-run SigMF recording >=2s at 240 kS/s (noise -- content doesn't
    matter for a transport-level test) with the recording.json index
    msdr.recorder.Recorder writes -- mirrors tests/api/test_recordings.py's
    _write_synthetic_recording, scaled up for a real VirtualReceiver tune."""
    d = root / "recordings" / name
    d.mkdir(parents=True)
    stem = f"run-01_{_SAMPLE_RATE_HZ}SPS_{_CENTER_HZ}Hz"
    samples = int(_SAMPLE_RATE_HZ * _DURATION_S)
    rng = np.random.default_rng(0)
    iq = (rng.standard_normal(samples * 2) * 3000).astype("<i2")
    (d / f"{stem}.sigmf-data").write_bytes(iq.tobytes())
    meta = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": _SAMPLE_RATE_HZ,
            "core:version": "1.0.0",
        },
        "captures": [{"core:sample_start": 0, "core:frequency": _CENTER_HZ}],
        "annotations": [],
    }
    (d / f"{stem}.sigmf-meta").write_text(json.dumps(meta))
    index = {
        "start_wall": 1_700_000_000.0,
        "end_wall": 1_700_000_000.0 + _DURATION_S,
        "preroll_s": 0,
        "runs": [
            {
                "data": f"{stem}.sigmf-data",
                "meta": f"{stem}.sigmf-meta",
                "sample_rate": _SAMPLE_RATE_HZ,
                "center_hz": _CENTER_HZ,
                "samples": samples,
                "start_wall": 1_700_000_000.0,
                "settings": {},
            }
        ],
    }
    (d / "recording.json").write_text(json.dumps(index))
    return d


def _settings(tmp_path: Path, *, port: int) -> Settings:
    # Every filesystem-backed default (recorder_root, tle/nextpass caches,
    # location file, decoder dirs) is redirected under tmp_path -- Settings'
    # own defaults point at the real user's Library/config dirs, which a test
    # must never touch (mirrors tests/api/test_app.py's running_app).
    return Settings(
        host="127.0.0.1",
        port=port,
        db_path=":memory:",
        tle_cache_dir=str(tmp_path / "tle"),
        nextpass_cache_dir=str(tmp_path / "nextpass_cache"),
        location_file=str(tmp_path / "location.json"),
        recorder_root=str(tmp_path / "recorder"),
        decoder=DecoderSettings(
            calls_dir=str(tmp_path / "decoder" / "calls"),
            fax_dir=str(tmp_path / "decoder" / "fax"),
        ),
        # Only `survey` (and review, which has no feature flag of its own)
        # need to be live for this test -- everything else is turned off to
        # keep the fake server's set_count() reading meaningful (nothing
        # unrelated should be touching the radio either) and the test fast.
        features=Features(
            recorder=False,
            decoder=False,
            beacons=False,
            scanner=False,
            tracking=False,
            passes=False,
            scans=False,
            sky=False,
            catalog=False,
            journal_passive=False,
            ident=False,
            nearby=False,
            detect=False,
            telemetry=False,
        ),
    )


def test_review_flow_end_to_end(tmp_path):
    server = _FakeServerThread()
    try:
        recording_dir = _write_synthetic_recording(tmp_path)
        settings = _settings(tmp_path, port=server.fake.port)
        db = Database(":memory:")
        adapter = SDRconnectAdapter(settings.host, settings.port, startup_grace_s=0.0)
        app = create_app(
            settings, adapter=adapter, db=db, settings_path=tmp_path / "settings.json"
        )

        with TestClient(app) as client:
            # -- 409 "no replay running" before any replay exists -----------
            resp = client.post(
                "/api/review/tune",
                json={
                    "frequency_hz": _CENTER_HZ,
                    "demodulator": "NFM",
                    "bandwidth_hz": _BANDWIDTH_HZ,
                },
            )
            assert resp.status_code == 409

            with client.websocket_connect("/ws/audio") as ws:
                ws.send_json({"type": "start"})
                msg = ws.receive_json()
                assert msg["type"] == "unavailable"

            # -- start the replay --------------------------------------------
            resp = client.post(
                "/api/survey/replay", json={"path": str(recording_dir), "loop": True}
            )
            assert resp.status_code == 200

            review = client.get("/api/review").json()
            assert review["replay"] is not None
            # A VirtualReceiver exists as soon as the replay starts (S3
            # contract), but sits idle until the first tune.
            assert review["receiver"]["state"] == "idle"
            assert review["bookmarks"] == []

            # -- tune ----------------------------------------------------------
            resp = client.post(
                "/api/review/tune",
                json={
                    "frequency_hz": _CENTER_HZ,
                    "demodulator": "NFM",
                    "bandwidth_hz": _BANDWIDTH_HZ,
                },
            )
            assert resp.status_code == 200
            assert client.get("/api/review").json()["receiver"]["state"] == "running"
            native = client.post(
                "/api/inspection/review/audio-dsp", json={"nr_strength": 0.7}
            )
            assert native.status_code == 200, native.text
            assert native.json()["receiver"]["native_audio"]["nr_strength"] == 0.7
            assert client.get("/api/review").json()["native_audio"]["nr_strength"] == 0.7

            # -- listen: >=3 binary frames, header <IIi (generation,
            # sample_index_lo32, rate) --------------------------------------
            with client.websocket_connect("/ws/audio") as ws:
                ws.send_json({"type": "start"})
                flush = ws.receive_json()
                assert flush["type"] == "flush"
                first_generation = flush["generation"]

                seen = []
                for _ in range(3):
                    raw = ws.receive_bytes()
                    assert len(raw) > 12
                    generation, _sample_index_lo, rate = struct.unpack("<IIi", raw[:12])
                    seen.append(generation)
                    assert rate == 48_000
                assert all(g == first_generation for g in seen)

                # -- seek: a flush frame, then no frame of the old generation
                resp = client.post("/api/review/seek", json={"position_s": 1.0})
                assert resp.status_code == 200

                flush2 = ws.receive_json()
                assert flush2["type"] == "flush"
                assert flush2["generation"] != first_generation

                for _ in range(3):
                    raw = ws.receive_bytes()
                    generation, _sample_index_lo, _rate = struct.unpack("<IIi", raw[:12])
                    assert generation == flush2["generation"]

            # -- loop -----------------------------------------------------------
            resp = client.post("/api/review/loop", json={"loop": False})
            assert resp.status_code == 200
            assert client.get("/api/review").json()["replay"]["loop"] is False

            # -- bookmarks: create / list / seek --------------------------------
            resp = client.post("/api/review/bookmarks", json={"note": "interesting burst"})
            assert resp.status_code == 200
            bookmark = resp.json()
            assert bookmark["provenance"] == "bookmark"

            listed = client.get(
                "/api/review/bookmarks", params={"path": str(recording_dir)}
            ).json()
            assert any(b["id"] == bookmark["id"] for b in listed)

            resp = client.post(f"/api/review/bookmarks/{bookmark['id']}/seek")
            assert resp.status_code == 200

            observations = client.get("/api/observations").json()
            assert any(o["id"] == bookmark["id"] for o in observations)

            # -- out-of-span tune -> 409 with a reason mentioning the span ------
            resp = client.post(
                "/api/review/tune",
                json={
                    "frequency_hz": _CENTER_HZ + 10_000_000,
                    "demodulator": "NFM",
                    "bandwidth_hz": _BANDWIDTH_HZ,
                },
            )
            assert resp.status_code == 409
            assert "span" in resp.json()["detail"].lower()

            # -- stop the replay: the receiver goes away, review goes idle ------
            resp = client.post("/api/survey/replay/stop")
            assert resp.status_code == 200
            assert client.get("/api/review").json()["receiver"] is None

        # -- the zero-live-writes gate: nothing above ever touched the radio --
        assert server.fake.set_count() == 0
    finally:
        server.stop()


def test_review_decode_start_stop_and_status(tmp_path):
    """S4 §4: POST /api/review/decode{,/stop}, GET /api/review's `decode`
    field -- 409 without a running replay, a real ReplayDecodeSession once
    one exists, and (like the S3 flow test above) zero writes to the fake
    radio the whole way through."""
    server = _FakeServerThread()
    try:
        recording_dir = _write_synthetic_recording(tmp_path)
        settings = _settings(tmp_path, port=server.fake.port)
        db = Database(":memory:")
        adapter = SDRconnectAdapter(settings.host, settings.port, startup_grace_s=0.0)
        app = create_app(settings, adapter=adapter, db=db, settings_path=tmp_path / "settings.json")

        with TestClient(app) as client:
            # -- 409 without a replay -----------------------------------------
            resp = client.post("/api/review/decode", json={"decoders": ["ais"]})
            assert resp.status_code == 409
            resp = client.post("/api/review/decode/stop")
            assert resp.status_code == 409

            # -- start the replay + tune NFM (ais's tap; built in, so it also runs in CI) --
            resp = client.post(
                "/api/survey/replay", json={"path": str(recording_dir), "loop": True}
            )
            assert resp.status_code == 200
            resp = client.post(
                "/api/review/tune",
                json={
                    "frequency_hz": _CENTER_HZ,
                    "demodulator": "NFM",
                    "bandwidth_hz": _BANDWIDTH_HZ,
                },
            )
            assert resp.status_code == 200

            assert client.get("/api/review").json()["decode"] is None

            # -- start single-mode decode --------------------------------------
            resp = client.post(
                "/api/review/decode", json={"decoders": ["ais"], "mode": "single"}
            )
            assert resp.status_code == 200
            status = resp.json()["decode"]
            assert status["mode"] == "single"
            assert status["source_id"].startswith("replay:")
            assert status["analysis_run_id"]
            ids = [d["id"] for d in status["decoders"]]
            assert ids == ["ais"]

            review = client.get("/api/review").json()
            assert review["decode"]["mode"] == "single"
            assert review["decode"]["analysis_run_id"] == status["analysis_run_id"]

            # -- stop --------------------------------------------------------
            resp = client.post("/api/review/decode/stop")
            assert resp.status_code == 200
            assert resp.json()["decode"] is None
            assert client.get("/api/review").json()["decode"] is None

            # -- stopping the replay tears any decode session down with it ----
            resp = client.post(
                "/api/review/decode", json={"decoders": ["ais"], "mode": "single"}
            )
            assert resp.status_code == 200
            resp = client.post("/api/survey/replay/stop")
            assert resp.status_code == 200
            resp = client.post("/api/review/decode/stop")
            assert resp.status_code == 409

        # -- the zero-live-writes gate: review decode never touches the radio --
        assert server.fake.set_count() == 0
    finally:
        server.stop()


def test_review_decode_lifecycle_serializes_start_stop_and_gates_stale_callbacks(
    monkeypatch, tmp_path
):
    """A stop racing decoder startup waits for startup, then tears down that
    exact session. Late callbacks from it cannot leak events/status afterward."""
    service = object.__new__(AppService)
    service._review_lifecycle_lock = asyncio.Lock()
    service._review_decode_generation = 0
    service._replay_decode = None
    service.virtual_rx = object()
    service.survey = SimpleNamespace(_replay=object())
    service.settings = SimpleNamespace(
        decoder=SimpleNamespace(bin_dirs=[], calls_dir=str(tmp_path / "calls"),
                                fax_dir=str(tmp_path / "fax"))
    )
    service._resolve_calls_dir = lambda settings: tmp_path / "calls"
    service._resolve_fax_dir = lambda settings: tmp_path / "fax"
    service.review_status = lambda: {"decode": None}
    published = []
    service._on_replay_decode_event = published.append
    service._publish_review_decode_status = published.append

    started = asyncio.Event()
    allow_start = asyncio.Event()
    stopped = asyncio.Event()
    sessions = []

    class FakeDecodeSession:
        def __init__(self, rx, replay, **kwargs):
            self.on_event = kwargs["on_event"]
            self.on_status = kwargs["on_status"]
            sessions.append(self)

        async def start_single(self, decoders):
            started.set()
            await allow_start.wait()

        async def start_band(self, decoders, max_channels, threshold_db):
            await self.start_single(decoders)

        async def stop(self):
            stopped.set()

    monkeypatch.setattr(review_module, "ReplayDecodeSession", FakeDecodeSession)
    monkeypatch.setattr(review_module, "resolve_binaries", lambda dirs: {})

    async def scenario():
        start_task = asyncio.create_task(service.review_decode_start(["ais"]))
        await started.wait()
        stop_task = asyncio.create_task(service.review_decode_stop())
        await asyncio.sleep(0)
        assert not stopped.is_set()
        allow_start.set()
        await start_task
        await stop_task
        assert stopped.is_set()
        sessions[0].on_event("late-event")
        sessions[0].on_status({"state": "late"})
        assert published == []

    asyncio.run(scenario())
