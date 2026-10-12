"""investigation workflow (lifecycle tests): a recording becomes an Observation, notes survive a
restart, deleted media is reported unavailable, clips, promote, export, and
stopping a replay under a review decode.

Every test uses a real on-disk DB in tmp and a real SDRconnectAdapter talking
to an in-process FakeSDRconnect, so `set_count()` proves nothing here writes
to the (fake) radio. The recorder is the real msdr.recorder.Recorder fed by
tests/recorder/conftest.py's FakeStream instead of SDRconnect's IQ socket.
"""

import asyncio
import contextlib
import json
from pathlib import Path

import httpx
import numpy as np

from msdr.api.app import create_app
from msdr.radio.fake_server import FakeSDRconnect
from msdr.radio.sdrconnect import SDRconnectAdapter
from msdr.recorder import Recorder, RecorderConfig
from msdr.settings import DecoderSettings, Features, Settings
from msdr.source.timeline import RecordingTimeline
from msdr.storage.db import Database
from tests.recorder.conftest import FakeStream
from tests.source.test_timeline import RUNS, write_three_runs

_RATE = 240_000
_FRAME = 24_000  # 0.1 s at _RATE


def _settings(tmp_path: Path, port: int, db_path: Path) -> Settings:
    # Same isolation as tests/api/test_review.py's _settings, plus the
    # recorder feature and a real db file (restart persistence).
    return Settings(
        host="127.0.0.1",
        port=port,
        db_path=str(db_path),
        tle_cache_dir=str(tmp_path / "tle"),
        nextpass_cache_dir=str(tmp_path / "nextpass_cache"),
        location_file=str(tmp_path / "location.json"),
        recorder_root=str(tmp_path / "recorder"),
        decoder=DecoderSettings(
            calls_dir=str(tmp_path / "decoder" / "calls"),
            fax_dir=str(tmp_path / "decoder" / "fax"),
        ),
        features=Features(
            recorder=True,
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


@contextlib.asynccontextmanager
async def _fake_radio():
    fake = FakeSDRconnect(port=0)
    fake.state["device_sample_rate"] = _RATE
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@contextlib.asynccontextmanager
async def _app(tmp_path: Path, fake: FakeSDRconnect, db_path: Path, stream: FakeStream):
    settings = _settings(tmp_path, fake.port, db_path)
    holder: dict = {}
    # Late-bound like the service's own _build_recorder wiring: the recorder
    # pushes every state change to AppService._publish_recorder_status.
    recorder = Recorder(
        RecorderConfig(
            root=Path(settings.recorder_root), preroll_s=0.5, min_free_gb=0, segment_s=0.05
        ),
        stream=stream,
        on_status=lambda status: holder["app"].state.service._publish_recorder_status(status),
    )
    adapter = SDRconnectAdapter(settings.host, settings.port, startup_grace_s=0.0)
    app = create_app(
        settings,
        adapter=adapter,
        db=Database(str(db_path)),
        settings_path=tmp_path / "settings.json",
        recorder=recorder,
    )
    holder["app"] = app  # app.state.service exists once the lifespan starts
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


async def _until(predicate, timeout: float = 5.0, step: float = 0.02):
    for _ in range(int(timeout / step)):
        if await predicate() if asyncio.iscoroutinefunction(predicate) else predicate():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not reached in time")


async def _push_frames(stream: FakeStream, n: int) -> None:
    for _ in range(n):
        stream.push(_FRAME)
        await asyncio.sleep(0.03)


async def _start_replay(client, path) -> None:
    resp = await client.post("/api/survey/replay", json={"path": str(path), "loop": True})
    assert resp.status_code == 200, resp.text


async def _pause_at(client, position_s: float) -> None:
    resp = await client.post(
        "/api/survey/replay/control", json={"playing": False, "position_s": position_s}
    )
    assert resp.status_code == 200, resp.text
    await asyncio.sleep(0.3)  # let the pump apply the pending seek


def _recording_under(tmp_path: Path) -> Path:
    """RUNS (tests/source/test_timeline.py): 3 runs, run 1 = 3000 samples at
    10 kS/s (0.3 s), I = local sample index, Q = run index."""
    root = tmp_path / "recorder" / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    return write_three_runs(root)


# -- 1. end-to-end acceptance ----------------------------------------------


async def test_record_note_bookmark_restart_and_return(tmp_path):
    db_path = tmp_path / "msdr.db"
    async with _fake_radio() as fake:
        stream = FakeStream()
        async with _app(tmp_path, fake, db_path, stream) as (app, client):
            service = app.state.service

            async def arm_ok():
                # The service's seed reads props into confirmed state without
                # forwarding them to the recorder (only later change events
                # are); mirror the seeded values the way those events would.
                for prop in ("started", "device_sample_rate", "device_center_frequency"):
                    if prop in service.confirmed.props:
                        service.recorder.on_property(prop, service.confirmed.props[prop])
                resp = await client.post("/api/recorder/arm", json={"on": True})
                return resp.status_code == 200

            await _until(arm_ok)  # 409 "radio not connected" until the seed lands
            await _push_frames(stream, 10)  # ~1 s of pre-roll (0.5 s kept)
            resp = await client.post("/api/recorder/start")
            assert resp.status_code == 200, resp.text
            sets_after_rec = fake.set_count()
            await _push_frames(stream, 10)
            resp = await client.post("/api/recorder/stop")
            assert resp.status_code == 200, resp.text
            await _until(
                lambda: service._rec_obs is None and service.recorder.status()["state"] == "armed"
            )

            recs = (await client.get("/api/recordings")).json()
            assert len(recs) == 1
            rec_dir = Path(recs[0]["path"])
            index = json.loads((rec_dir / "recording.json").read_text())

            obs_list = (await client.get("/api/observations")).json()
            rec_obs = [o for o in obs_list if o["provenance"] == "recording"]
            assert len(rec_obs) == 1
            obs = rec_obs[0]
            assert obs["closed"] is True
            assert obs["capture_quality"] == []
            assert obs["reported_hz"] == 145_800_000
            assert obs["bandwidth_hz"] == 12_500
            [ref] = obs["refs"]
            assert Path(ref["path"]) == rec_dir and ref["name"] == rec_dir.name
            assert ref["run_index"] == 0 and ref["sample_start"] == 0
            assert ref["sample_end"] == obs["sample_end"] == index["runs"][0]["samples"]
            # Honest start: first promoted pre-roll sample, before REC.
            assert obs["start_ts"] == index["runs"][0]["start_wall"]
            assert obs["start_ts"] < index["start_wall"]
            assert obs["end_ts"] == index["end_wall"]

            resp = await client.patch(f"/api/observations/{obs['id']}", json={"notes": "NOAA test"})
            assert resp.status_code == 200
            assert resp.json()["notes"] == "NOAA test"

            await _start_replay(client, rec_dir)
            await _pause_at(client, 0.5)
            resp = await client.post("/api/review/bookmarks", json={"note": "half a second"})
            assert resp.status_code == 200
            bookmark = resp.json()

        # -- restart: same db file -----------------------------------------
        async with _app(tmp_path, fake, db_path, FakeStream()) as (app2, client2):
            resp = await client2.get(f"/api/observations/{obs['id']}")
            assert resp.status_code == 200
            again = resp.json()
            assert again["notes"] == "NOAA test"
            assert [r["available"] for r in again["refs"]] == [True]

            await _start_replay(client2, rec_dir)
            await _pause_at(client2, 0.0)
            resp = await client2.post(f"/api/review/bookmarks/{bookmark['id']}/seek")
            assert resp.status_code == 200, resp.text
            await asyncio.sleep(0.3)
            replay = (await client2.get("/api/review")).json()["replay"]
            expected = bookmark["refs"][0]["sample_start"] / _RATE
            block_s = max(512, _RATE / 20) / _RATE
            assert abs(replay["position_s"] - expected) <= block_s
            assert abs(expected - 0.5) <= block_s

        # Replay, notes, bookmark, restart: none of it wrote to the radio.
        assert fake.set_count() == sets_after_rec


async def test_radio_stop_during_recording_closes_with_missing_frames(tmp_path):
    """Auto-disarm (radio stopped under a recording): the observation is
    closed, never left open, and marked missing_frames with the reason."""
    async with _fake_radio() as fake:
        stream = FakeStream()
        async with _app(tmp_path, fake, tmp_path / "msdr.db", stream) as (app, client):
            service = app.state.service

            async def arm_ok():
                for prop in ("started", "device_sample_rate", "device_center_frequency"):
                    if prop in service.confirmed.props:
                        service.recorder.on_property(prop, service.confirmed.props[prop])
                resp = await client.post("/api/recorder/arm", json={"on": True})
                return resp.status_code == 200

            await _until(arm_ok)
            await _push_frames(stream, 3)
            assert (await client.post("/api/recorder/start")).status_code == 200
            await _push_frames(stream, 3)
            # What SDRconnect's Stop looks like to msdr: confirmed state, then
            # the forwarded property.
            service.confirmed.props["started"] = False
            service.recorder.on_property("started", False)
            await _until(
                lambda: service._rec_obs is None and service.recorder.status()["state"] == "off"
            )
            [obs] = [
                o
                for o in (await client.get("/api/observations")).json()
                if o["provenance"].startswith("recording")
            ]
            assert obs["closed"] is True
            assert obs["capture_quality"] == ["missing_frames"]
            assert "radio stopped" in obs["provenance"]
        assert fake.set_count() == 0


# -- 2. deleted recording ---------------------------------------------------


async def test_deleted_recording_refs_unavailable_and_seek_404(tmp_path):
    rec_dir = _recording_under(tmp_path)
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (_app_, client):
            await _start_replay(client, rec_dir)
            await _pause_at(client, 0.1)
            bookmark = (await client.post("/api/review/bookmarks", json={"note": "x"})).json()
            await client.post("/api/survey/replay/stop")

            resp = await client.delete(f"/api/recordings/{rec_dir.name}")
            assert resp.status_code == 200
            assert not rec_dir.exists()

            got = (await client.get(f"/api/observations/{bookmark['id']}")).json()
            assert [r["available"] for r in got["refs"]] == [False]
            resp = await client.post(f"/api/review/bookmarks/{bookmark['id']}/seek")
            assert resp.status_code == 404
            assert resp.json()["detail"] == "recording no longer exists"
        assert fake.set_count() == 0


# -- 3. clips ----------------------------------------------------------------


async def test_clip_create_list_consistent_and_refusals(tmp_path):
    rec_dir = _recording_under(tmp_path)
    rate, center, _n, start_wall = RUNS[0]
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (app, client):
            # No replay: 409.
            resp = await client.post("/api/review/clips", json={"start_s": 0.1, "end_s": 0.2})
            assert resp.status_code == 409

            await _start_replay(client, rec_dir)
            resp = await client.post(
                "/api/review/clips", json={"start_s": 0.1, "end_s": 0.25, "note": "burst"}
            )
            assert resp.status_code == 200, resp.text
            clip = resp.json()
            assert clip["provenance"] == "clip" and clip["notes"] == "burst"
            clip_dir = Path(clip["clip_path"])
            assert clip_dir == tmp_path / "recorder" / "clips" / "rec_100-250"

            # SigMF pair consistent: samples == (end - start) * rate, content
            # is exactly the parent's run-1 samples 1000..2500.
            stem = f"run-01_{rate}SPS_{center}Hz"
            meta = json.loads((clip_dir / f"{stem}.sigmf-meta").read_text())
            data = np.fromfile(clip_dir / f"{stem}.sigmf-data", dtype="<i2")
            assert meta["global"]["core:sample_rate"] == rate
            assert meta["global"]["core:datatype"] == "ci16_le"
            assert meta["captures"][0]["core:frequency"] == center
            assert data.size // 2 == round((0.25 - 0.1) * rate) == 1500
            assert (data[0::2] == np.arange(1000, 2500)).all()
            assert (data[1::2] == 0).all()
            info = json.loads((clip_dir / "clip.json").read_text())
            assert info["parent_path"] == str(rec_dir)
            assert (info["run_index"], info["sample_start"], info["sample_end"]) == (0, 1000, 2500)
            assert info["note"] == "burst"
            assert info["capture_start"] == start_wall + 0.1

            # Observation with two refs: the clip (available) and the parent range.
            by_path = {r["path"]: r for r in clip["refs"]}
            assert set(by_path) == {str(clip_dir), str(rec_dir)}
            assert by_path[str(clip_dir)]["available"] is True
            assert by_path[str(clip_dir)]["sample_end"] == 1500
            parent = by_path[str(rec_dir)]
            assert (parent["run_index"], parent["sample_start"], parent["sample_end"]) == (
                0,
                1000,
                2500,
            )

            listed = (await client.get("/api/review/clips", params={"path": str(rec_dir)})).json()
            assert [c["id"] for c in listed] == [clip["id"]]
            assert listed[0]["clip_path"] == str(clip_dir)

            # Across the run-1/run-2 boundary (0.3 s): 409 with the reason.
            resp = await client.post("/api/review/clips", json={"start_s": 0.2, "end_s": 0.35})
            assert resp.status_code == 409
            assert "run" in resp.json()["detail"]
            # Bad ranges: 409.
            for body in ({"start_s": 0.2, "end_s": 0.2}, {"start_s": 0.0, "end_s": 601.0}):
                assert (await client.post("/api/review/clips", json=body)).status_code == 409

            # Unwritable clips_dir (its parent is a regular file): 503 + OS text.
            blocker = tmp_path / "not-a-dir"
            blocker.write_text("")
            app.state.service.settings.clips_dir = str(blocker / "clips")
            resp = await client.post("/api/review/clips", json={"start_s": 0.0, "end_s": 0.1})
            assert resp.status_code == 503
            assert "clip write failed" in resp.json()["detail"]
            assert str(blocker) in resp.json()["detail"]
        assert fake.set_count() == 0


async def test_clip_preserves_overlapping_annotations_and_gap_timing(tmp_path):
    rec_dir = _recording_under(tmp_path)
    meta_path = sorted(rec_dir.glob("*.sigmf-meta"))[0]
    parent_meta = json.loads(meta_path.read_text())
    parent_meta["annotations"].extend(
        [
            {"core:sample_start": 1250, "core:label": "signal", "core:comment": "burst"},
            {
                "core:sample_start": 900,
                "core:sample_count": 300,
                "core:label": "region",
                "core:comment": "starts before clip",
            },
            {
                "core:sample_start": 1500,
                "core:label": "gap",
                "core:comment": "missing ~500 samples",
            },
            {
                "core:sample_start": 2400,
                "core:sample_count": 300,
                "core:label": "region",
                "core:comment": "ends after clip",
            },
        ]
    )
    meta_path.write_text(json.dumps(parent_meta))
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (_app_, client):
            await _start_replay(client, rec_dir)
            # .1.. .3 s includes 1500 stored samples and a .05 s gap.
            resp = await client.post("/api/review/clips", json={"start_s": 0.1, "end_s": 0.3})
            assert resp.status_code == 200, resp.text
            clip = resp.json()
            clip_dir = Path(clip["clip_path"])
            stem = "run-01_10000SPS_100000000Hz"
            meta = json.loads((clip_dir / f"{stem}.sigmf-meta").read_text())
            info = json.loads((clip_dir / "clip.json").read_text())
            data = np.fromfile(clip_dir / f"{stem}.sigmf-data", dtype="<i2")
            assert (info["sample_start"], info["sample_end"], info["samples"]) == (
                1000,
                2500,
                1500,
            )
            assert data.size // 2 == 1500
            assert (data[0::2] == np.arange(1000, 2500)).all()
            assert meta["annotations"] == [
                {"core:sample_start": 250, "core:label": "signal", "core:comment": "burst"},
                {
                    "core:sample_start": 0,
                    "core:sample_count": 200,
                    "core:label": "region",
                    "core:comment": "starts before clip",
                },
                {
                    "core:sample_start": 500,
                    "core:label": "gap",
                    "core:comment": "missing ~500 samples",
                },
                {
                    "core:sample_start": 1400,
                    "core:sample_count": 100,
                    "core:label": "region",
                    "core:comment": "ends after clip",
                },
            ]
            # The exported clip is itself replayable with the same interior gap.
            exported = RecordingTimeline.from_path(clip_dir)
            assert exported.runs[0].interior_gaps == ((500, 500),)
            assert exported.total_elapsed_s == 0.2

            # A selection wholly after the gap starts at the correct stored
            # index and carries the elapsed capture time, without a false gap.
            resp = await client.post("/api/review/clips", json={"start_s": 0.25, "end_s": 0.3})
            assert resp.status_code == 200, resp.text
            postgap_dir = Path(resp.json()["clip_path"])
            postgap_info = json.loads((postgap_dir / "clip.json").read_text())
            postgap_meta = json.loads((postgap_dir / f"{stem}.sigmf-meta").read_text())
            assert (postgap_info["sample_start"], postgap_info["sample_end"]) == (2000, 2500)
            assert postgap_info["capture_start"] == RUNS[0][3] + 0.25
            assert not any(a.get("core:label") == "gap" for a in postgap_meta["annotations"])

            # Ending at the gap does not claim its missing interval as clip time.
            resp = await client.post("/api/review/clips", json={"start_s": 0.1, "end_s": 0.15})
            assert resp.status_code == 200, resp.text
            before_gap = resp.json()
            before_gap_dir = Path(before_gap["clip_path"])
            before_gap_meta = json.loads((before_gap_dir / f"{stem}.sigmf-meta").read_text())
            assert abs(before_gap["end_ts"] - (RUNS[0][3] + 0.15)) < 1e-6
            assert not any(a.get("core:label") == "gap" for a in before_gap_meta["annotations"])
        assert fake.set_count() == 0


# -- 4. promote --------------------------------------------------------------


async def test_promote_observation_creates_linked_signal(tmp_path):
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (app, client):
            from msdr.observations.models import Observation

            repo = app.state.service.observations
            obs = repo.add(
                Observation(
                    reported_hz=162_400_000,
                    bandwidth_hz=25_000,
                    receiver={"demodulator": "NFM"},
                    provenance="recording",
                    notes="NOAA test",
                )
            )
            resp = await client.post(f"/api/observations/{obs.id}/promote", json={"name": "WX1"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["note"] == "link suggests, does not prove identity"
            signal = body["signal"]
            assert signal["name"] == "WX1"
            assert signal["frequency_hz"] == 162_400_000
            assert signal["status"] == "unknown"
            assert signal["notes"] == "NOAA test"
            assert signal["filter_bandwidth_hz"] == 25_000
            assert body["observation"]["signal_id"] == signal["id"]
            assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 200
            assert repo.get(obs.id).signal_id == signal["id"]

            # PATCH notes alone leaves the memory link alone.
            resp = await client.patch(f"/api/observations/{obs.id}", json={"notes": "edited"})
            assert resp.json()["signal_id"] == signal["id"]
            assert (await client.patch(f"/api/observations/{obs.id}", json={})).status_code == 422
            assert (await client.post("/api/observations/nope/promote")).status_code == 404


async def test_promote_observation_carries_receiver_snapshot_antenna(tmp_path):
    # receiver is keyed by wire property name (AppService._session_receiver_
    # snapshot), "active_antenna" -- not the Signal field name "antenna".
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (app, client):
            from msdr.observations.models import Observation

            repo = app.state.service.observations
            obs = repo.add(
                Observation(
                    reported_hz=162_400_000,
                    bandwidth_hz=25_000,
                    receiver={"demodulator": "NFM", "active_antenna": "Antenna C"},
                    provenance="recording",
                )
            )
            resp = await client.post(f"/api/observations/{obs.id}/promote", json={})
            assert resp.status_code == 200, resp.text
            assert resp.json()["signal"]["antenna"] == "Antenna C"


# -- 5. export ---------------------------------------------------------------


async def test_export_contains_superseded_claims(tmp_path):
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (app, client):
            from msdr.observations.models import Observation

            obs = app.state.service.observations.add(
                Observation(reported_hz=144_800_000, provenance="bookmark")
            )
            first = await client.post(
                f"/api/observations/{obs.id}/claims",
                json={"subject": "service", "value": "APRS", "rationale": "heard it"},
            )
            old_id = first.json()["claims"][0]["claim_id"]
            await client.post(
                f"/api/observations/{obs.id}/claims",
                json={
                    "subject": "service",
                    "value": "packet",
                    "rationale": "second look",
                    "supersedes": old_id,
                },
            )
            resp = await client.get(f"/api/observations/{obs.id}/export")
            assert resp.status_code == 200
            doc = resp.json()
            assert set(doc) >= {
                "observation",
                "claims",
                "refs",
                "decode_events",
                "session",
                "exported_at",
                "msdr_version",
            }
            values = {c["value"]: c for c in doc["claims"]}
            assert values["APRS"]["historical"] is True
            assert values["APRS"]["superseded_by"] == values["packet"]["claim_id"]
            assert values["packet"]["historical"] is False

            many = (await client.get("/api/observations/export", params={"limit": 10})).json()
            assert obs.id in [d["observation"]["id"] for d in many]
            later = await client.get("/api/observations/export", params={"since_ts": 4e9})
            assert later.json() == []


# -- 6. stop replay under a review decode ------------------------------------


async def test_stop_replay_stops_review_decode(tmp_path):
    rec_dir = _recording_under(tmp_path)
    async with _fake_radio() as fake:
        async with _app(tmp_path, fake, tmp_path / "msdr.db", FakeStream()) as (_app_, client):
            await _start_replay(client, rec_dir)
            resp = await client.post(
                "/api/review/tune",
                json={"frequency_hz": 100_000_000, "demodulator": "NFM", "bandwidth_hz": 3_000},
            )
            assert resp.status_code == 200, resp.text
            resp = await client.post("/api/review/decode", json={"decoders": ["ais"]})
            assert resp.status_code == 200, resp.text
            assert resp.json()["decode"] is not None

            resp = await client.post("/api/survey/replay/stop")
            assert resp.status_code == 200
            review = (await client.get("/api/review")).json()
            assert review["decode"] is None
            assert review["receiver"] is None
        assert fake.set_count() == 0
