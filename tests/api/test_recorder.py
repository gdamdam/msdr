"""Pre-roll IQ recorder API: GET /api/recorder, POST /api/recorder/{arm,start,stop},
the recorder feature flag, RecorderError -> 409, receiver-property forwarding, and
startup re-arm. Uses a fake recorder (msdr.recorder isn't this task's package --
see the design doc) rather than the real msdr.recorder.Recorder, and its own
`running_app` helper (create_app's `recorder=` injection point, mirroring how
tests/api/test_app.py injects a fake adapter)."""

import asyncio
import contextlib
import sys
from pathlib import Path

import httpx

from msdr.api.app import create_app
from msdr.api.service import RecorderError
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database


class FakeRecorder:
    """Same surface as msdr.recorder.Recorder (see the design doc's API/UI
    sections): start/stop, arm/record/stop_recording returning a status dict,
    on_property forwarding, and status()."""

    def __init__(self, *, fail_arm: bool = False) -> None:
        self.running = False
        self.armed = False
        self.state = "off"
        self.arm_calls: list[bool] = []
        self.record_calls = 0
        self.stop_calls = 0
        self.properties: list[tuple[str, object]] = []
        self._fail_arm = fail_arm

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def arm(self, on: bool) -> dict:
        if self._fail_arm:
            raise RecorderError("arm refused: low disk")
        self.armed = on
        self.arm_calls.append(on)
        self.state = "armed" if on else "off"
        return self.status()

    async def record(self) -> dict:
        self.record_calls += 1
        self.state = "recording"
        return self.status()

    async def stop_recording(self) -> dict:
        self.stop_calls += 1
        self.state = "armed" if self.armed else "off"
        return self.status()

    def on_property(self, prop: str, value: object) -> None:
        self.properties.append((prop, value))

    def status(self) -> dict:
        return {
            "state": self.state,
            "armed": self.armed,
            "buffered_s": 12.0,
            "recording_s": 0.0,
            "preroll_s": 300,
            "sample_rate": 2_000_000,
            "center_hz": 145_800_000,
            "free_gb": 123.4,
            "root": "/tmp/fake-recorder",
            "last_recording": None,
            "error": None,
        }


@contextlib.asynccontextmanager
async def running_app(
    recorder=None, adapter: FakeAdapter | None = None, settings: Settings | None = None
):
    adapter = adapter if adapter is not None else FakeAdapter()
    if settings is None:
        settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db, recorder=recorder)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client, adapter


async def test_recorder_status_route_reports_fake_state():
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.get("/api/recorder")
        assert resp.status_code == 200
        assert resp.json()["state"] == "off"
        assert resp.json()["root"] == "/tmp/fake-recorder"


async def test_recorder_arm_calls_recorder_and_persists_setting():
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.post("/api/recorder/arm", json={"on": True})
        assert resp.status_code == 200
        assert resp.json()["armed"] is True
        assert fake.arm_calls == [True]
        assert app.state.service.settings.recorder_armed is True

        resp = await client.post("/api/recorder/arm", json={"on": False})
        assert resp.status_code == 200
        assert fake.arm_calls == [True, False]
        assert app.state.service.settings.recorder_armed is False


async def test_recorder_start_and_stop_delegate_to_record_and_stop_recording():
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, _adapter):
        await client.post("/api/recorder/arm", json={"on": True})

        resp = await client.post("/api/recorder/start")
        assert resp.status_code == 200
        assert fake.record_calls == 1
        assert resp.json()["state"] == "recording"

        resp = await client.post("/api/recorder/stop")
        assert resp.status_code == 200
        assert fake.stop_calls == 1


async def test_recorder_error_maps_to_409():
    fake = FakeRecorder(fail_arm=True)
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.post("/api/recorder/arm", json={"on": True})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "arm refused: low disk"
        # A failed arm must not have persisted recorder_armed.
        assert app.state.service.settings.recorder_armed is False


async def test_recorder_folder_patch_rebuilds_idle_runtime_and_keeps_old_files(
    tmp_path, monkeypatch
):
    old_root = tmp_path / "old"
    old_recording = old_root / "recordings" / "previous" / "recording.json"
    old_recording.parent.mkdir(parents=True)
    old_recording.write_text("{}")
    new_root = tmp_path / "new"
    initial = FakeRecorder()
    settings = Settings(db_path=":memory:", recorder_root=str(old_root))

    class RootRecorder(FakeRecorder):
        def __init__(self, root):
            super().__init__()
            self.root = root

        def status(self):
            return {**super().status(), "root": self.root}

    async with running_app(initial, settings=settings) as (app, client, _adapter):
        service = app.state.service
        monkeypatch.setattr(
            service, "_build_recorder", lambda config: RootRecorder(config.recorder_root)
        )
        resp = await client.patch("/api/settings", json={"recorder_root": str(new_root)})
        assert resp.status_code == 200
        assert resp.json()["recorder_root"] == str(new_root)
        assert new_root.is_dir()
        assert initial.running is False
        assert service.recorder.running is True
        properties = dict(service.recorder.properties)
        assert properties["connected"] == service.adapter.connected
        for name in ("started", "device_sample_rate", "device_center_frequency"):
            assert properties[name] == service.confirmed.props[name]
        assert (await client.get("/api/recorder")).json()["root"] == str(new_root)
        assert old_recording.read_text() == "{}"


async def test_recorder_root_patch_rebuilds_wifi_sessions_root_and_refuses_active_session(
    tmp_path,
):
    from msdr.settings import WifiSettings

    helper = Path(__file__).resolve().parent.parent / "wifi" / "fake_helper.py"
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    settings = Settings(
        db_path=":memory:", recorder_root=str(old_root),
        wifi=WifiSettings(enabled=True, helper_command=[sys.executable, str(helper)]),
    )
    async with running_app(FakeRecorder(), settings=settings) as (app, client, _adapter):
        service = app.state.service
        assert service.wifi_sessions.root == service._wifi_sessions_root()
        changed = await client.patch("/api/settings", json={"recorder_root": str(new_root)})
        assert changed.status_code == 200
        assert service.wifi_sessions.root == service._wifi_sessions_root()

        assert (await client.post("/api/wifi/start", json={"channel": 6})).status_code == 200
        assert (await client.post("/api/wifi/sessions/start")).status_code == 200
        refused = await client.patch(
            "/api/settings", json={"recorder_root": str(tmp_path / "third")}
        )
        assert refused.status_code == 409
        assert service.wifi_sessions.root == service._wifi_sessions_root()
        await client.post("/api/wifi/sessions/stop")
        await client.post("/api/wifi/stop")


async def test_recorder_folder_patch_rejects_busy_recorder_and_capture(tmp_path, monkeypatch):
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    initial = FakeRecorder()
    settings = Settings(db_path=":memory:", recorder_root=str(old_root))
    async with running_app(initial, settings=settings) as (app, client, _adapter):
        service = app.state.service
        monkeypatch.setattr(service, "_build_recorder", lambda _: (_ for _ in ()).throw(
            AssertionError("busy recorder must not be replaced")))
        for state in ("armed", "recording", "finalizing", "error"):
            initial.state = state
            resp = await client.patch("/api/settings", json={"recorder_root": str(new_root)})
            assert resp.status_code == 409
            assert service.settings.recorder_root == str(old_root)
        initial.state = "off"
        service.survey._replay = object()
        resp = await client.patch("/api/settings", json={"recorder_root": str(new_root)})
        assert resp.status_code == 409
        assert "playback" in resp.json()["detail"]
        service.survey._replay = None
        service._validation_busy = True
        resp = await client.patch("/api/settings", json={"recorder_root": str(new_root)})
        assert resp.status_code == 409
        assert "validation capture" in resp.json()["detail"]
        assert not new_root.exists()


async def test_recorder_folder_patch_validates_path_before_runtime_change(tmp_path):
    initial = FakeRecorder()
    settings = Settings(db_path=":memory:", recorder_root=str(tmp_path / "old"))
    bad_file = tmp_path / "file"
    bad_file.write_text("not a directory")
    async with running_app(initial, settings=settings) as (app, client, _adapter):
        for invalid in ("relative/folder", str(bad_file)):
            resp = await client.patch("/api/settings", json={"recorder_root": invalid})
            assert resp.status_code == 409
            assert app.state.service.settings.recorder_root == settings.recorder_root
            assert app.state.service.recorder is initial
            assert initial.running is True


async def test_recorder_feature_disabled_gates_every_route():
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, _adapter):
        resp = await client.patch("/api/settings", json={"features": {"recorder": False}})
        assert resp.status_code == 200

        assert (await client.get("/api/recorder")).status_code == 404
        assert (await client.post("/api/recorder/arm", json={"on": True})).status_code == 404
        assert (await client.post("/api/recorder/start")).status_code == 404
        assert (await client.post("/api/recorder/stop")).status_code == 404


async def test_recorder_forwards_primary_device_properties_only():
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, adapter):
        await adapter.external_change("device_vfo_frequency", 146_000_000)
        await asyncio.sleep(0.05)
        assert ("device_vfo_frequency", 146_000_000) in fake.properties

        # signal_power isn't in the recorder's forwarded-props list.
        before = len(fake.properties)
        await adapter.external_change("signal_power", -70.0)
        await asyncio.sleep(0.05)
        assert len(fake.properties) == before


async def test_recorder_rearms_on_startup_when_previously_armed():
    fake = FakeRecorder()
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    settings.recorder_armed = True
    # FakeAdapter starts out connected and started, so AppService.start()'s
    # own seed path (not a later ConnectionChanged) is what triggers this.
    async with running_app(fake, settings=settings) as (app, client, _adapter):
        assert fake.arm_calls == [True]


async def test_recorder_tick_publishes_status_while_armed():
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, _adapter):
        service = app.state.service
        q = service.broadcaster.subscribe()
        await client.post("/api/recorder/arm", json={"on": True})
        await asyncio.sleep(1.2)  # > _RECORDER_TICK_INTERVAL_S

        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        assert any(m["type"] == "recorder" and m["state"] == "armed" for m in msgs)
        service.broadcaster.unsubscribe(q)


async def test_seed_forwards_startup_properties_to_recorder():
    # A radio already playing at connect time used to leave the recorder
    # with started=False and no rate/centre until a *change* event arrived
    # (workflow review), so ARM was refused. The seed must reach it too.
    fake = FakeRecorder()
    async with running_app(fake) as (app, client, _adapter):
        seeded = dict(fake.properties)
        assert "started" in seeded
        assert "device_sample_rate" in seeded
        assert "device_center_frequency" in seeded
