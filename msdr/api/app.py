"""FastAPI application: REST + WebSocket front end over AppService.

The only module allowed to import fastapi/starlette/uvicorn in msdr.
"""

import asyncio
import contextlib
import dataclasses
import sqlite3
import struct
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from msdr.api.schemas import (
    AllocationRefreshIn,
    AntennaIn,
    BandSaveIn,
    BandStartIn,
    BeaconStartIn,
    BookmarkIn,
    BulkOpIn,
    CatalogImportIn,
    CatalogRefreshIn,
    ClipIn,
    DecoderPrepareIn,
    DecoderRestoreIn,
    DecoderStartIn,
    FindPatchIn,
    FindPromoteIn,
    LabelIn,
    LibraryRedoIn,
    ListIn,
    ListItemsIn,
    ListItemSkipPatch,
    ListOrderIn,
    ListPatch,
    MergeIn,
    NativeAudioPatch,
    ObservationPatch,
    ObservationPromoteIn,
    OperatorClaimIn,
    ProfileIn,
    ProfilePatch,
    ReceiverAudioDspIn,
    RecorderArmIn,
    ReviewAudioIn,
    ReviewDecodeIn,
    ReviewLoopIn,
    ReviewSeekIn,
    ReviewTuneIn,
    RuleIn,
    RulePatch,
    SavedSearchIn,
    ScanRunIn,
    ScanSkipIn,
    ScanStartIn,
    SettingsPatch,
    SignalIn,
    SignalPatch,
    SurveyBandScanStartIn,
    SurveyDecodeIn,
    SurveyDimensionsIn,
    SurveyReplayControlIn,
    SurveyReplayIn,
    SurveySaveIn,
    SurveySpanIn,
    SurveyTestSignalIn,
    SurveyUndecodeIn,
    TagIn,
    TagPatch,
    TuneFrequencyIn,
    WifiStartIn,
    WifiSurveyIn,
    restore_report_to_dict,
)
from msdr.api.service import (
    AppService,
    BandScanError,
    BeaconError,
    DecoderError,
    ReceiverStopped,
    RecorderError,
    RestoreRunning,
    SurveyError,
    next_outgoing,
)
from msdr.engine.workbench import DriftConfig, LiveInspectionConfig, ScopeConfig, WatchConfig
from msdr.library.frequency import format_mhz, parse_frequency
from msdr.library.models import ReceiverProfile, Signal, Tag
from msdr.library.search import nearby, rank
from msdr.providers.base import IdentifyQuery
from msdr.radio.protocol import RadioAdapter
from msdr.radio.sdrconnect import SDRconnectAdapter
from msdr.settings import Settings
from msdr.storage.db import Database
from msdr.storage.transfer import export_all, import_all

#: POST /api/wifi/import body cap (docs/wifi.md).
WIFI_IMPORT_MAX_BYTES = 200 * 1024 * 1024

# The four pcap global-header magic numbers (msdr.wifi.pcap._MAGICS, kept
# private there -- duplicated here rather than widening that module's API
# just for this sniff check).
_PCAP_MAGICS = {
    b"\xa1\xb2\xc3\xd4",
    b"\xd4\xc3\xb2\xa1",
    b"\xa1\xb2\x3c\x4d",
    b"\x4d\x3c\xb2\xa1",
}


def _origin_is_local(origin: str | None) -> bool:
    """True unless `origin` names a host other than 127.0.0.1/localhost.

    This is the only Origin/CSRF check the API has (F6): without it, any web
    page can open the WS or POST to it cross-origin since it's plain HTTP on
    localhost with no auth. No Origin header at all -- curl, and every httpx
    test client call -- is allowed; a same-origin fetch from the served UI
    sends an Origin that matches, so this only ever blocks a foreign page.
    """
    if origin is None:
        return True
    return urlsplit(origin).hostname in ("127.0.0.1", "localhost")


def create_app(
    settings: Settings,
    adapter: RadioAdapter | None = None,
    db: Database | None = None,
    settings_path: Path | None = None,
    # Pre-roll recorder, injectable for tests -- same idea as `adapter`.
    recorder=None,
    # Signal decoder manager, injectable for tests -- same idea as `recorder`.
    decoder=None,
    # Wide-view survey, injectable for tests -- same idea as `decoder`.
    survey=None,
    # Beacon monitor, injectable for tests -- same idea as `survey`.
    beacons=None,
) -> FastAPI:
    adapter = adapter if adapter is not None else SDRconnectAdapter(settings.host, settings.port)
    db = db if db is not None else Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = AppService(
            settings,
            adapter,
            db,
            settings_path,
            recorder=recorder,
            decoder=decoder,
            survey=survey,
            beacons=beacons,
        )
        app.state.service = service
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(ConnectionError)
    async def _connection_error_handler(request: Request, exc: ConnectionError):
        return JSONResponse(status_code=503, content={"detail": "SDRconnect not connected"})

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(request: Request, exc: ValidationError):
        # Routes re-validate a merged patch as a whole model. That merge is
        # still client input, so a bad one is a 422 like any other body error
        # -- not a 500 out of the model layer.
        return JSONResponse(
            status_code=422,
            content={
                "detail": exc.errors(include_url=False, include_context=False, include_input=False)
            },
        )

    @app.exception_handler(RestoreRunning)
    async def _restore_running_handler(request: Request, exc: RestoreRunning):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ReceiverStopped)
    async def _receiver_stopped_handler(request: Request, exc: ReceiverStopped):
        return JSONResponse(
            status_code=503,
            content={"detail": "Receiver stopped: press Play in SDRconnect"},
        )

    @app.exception_handler(RecorderError)
    async def _recorder_error_handler(request: Request, exc: RecorderError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DecoderError)
    async def _decoder_error_handler(request: Request, exc: DecoderError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SurveyError)
    async def _survey_error_handler(request: Request, exc: SurveyError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(BandScanError)
    async def _band_scan_error_handler(request: Request, exc: BandScanError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(BeaconError)
    async def _beacon_error_handler(request: Request, exc: BeaconError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.middleware("http")
    async def _check_origin(request: Request, call_next):
        # Non-GET /api/* only: GETs are side-effect-free, and the WS
        # handshake is checked separately below (this middleware doesn't run
        # for websocket scopes).
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        if request.method != "GET" and is_api:
            if not _origin_is_local(request.headers.get("origin")):
                return JSONResponse(status_code=403, content={"detail": "origin not allowed"})
        response = await call_next(request)
        if not is_api:
            # The UI is plain ES modules with no cache-busting hashes, so after an
            # upgrade a tab could keep running old JS against a new API. no-cache
            # keeps the ETag round-trip but forces revalidation on every load.
            response.headers["Cache-Control"] = "no-cache"
        return response

    def get_service(request: Request) -> AppService:
        return request.app.state.service

    def _signals_with_list_ids(service: AppService, signals: list[Signal]) -> list[dict]:
        # One batched query for the whole page/result rather than one per
        # signal (mirrors SignalRepo.query()'s own tag-loading pattern).
        list_ids_by_signal = service.lists.list_ids_for([s.id for s in signals])
        return [
            {**s.model_dump(mode="json"), "list_ids": list_ids_by_signal.get(s.id, [])}
            for s in signals
        ]

    # --- state / capture ----------------------------------------------------

    @app.get("/api/state")
    async def get_state(service: AppService = Depends(get_service)):
        return service.state_payload()

    @app.get("/api/integrations")
    async def integrations_status(service: AppService = Depends(get_service)):
        return service.integrations.status()

    @app.get("/api/integrations/messages")
    async def integration_messages(
        limit: int = Query(default=100, ge=1, le=200),
        service: AppService = Depends(get_service),
    ):
        return service.integrations.messages(limit)

    @app.post("/api/connection/retry")
    async def retry_connection(service: AppService = Depends(get_service)):
        return service.retry_connection()

    @app.post("/api/capture")
    async def capture(service: AppService = Depends(get_service)):
        signal = await service.capture()
        return signal.model_dump(mode="json")

    # --- signals (inbox/recent declared before {id}) ------------------------

    @app.get("/api/signals/inbox")
    async def signals_inbox(service: AppService = Depends(get_service)):
        return _signals_with_list_ids(service, service.signals.inbox())

    @app.get("/api/signals/recent")
    async def signals_recent(service: AppService = Depends(get_service)):
        return _signals_with_list_ids(service, service.signals.recent())

    @app.get("/api/signals")
    async def list_signals(
        status: str | None = None,
        q: str | None = None,
        near: int | None = None,
        limit: int = 500,
        service: AppService = Depends(get_service),
    ):
        signals = service.signals.list(status=status, q=q, limit=limit)
        if near is not None:
            signals = nearby(signals, near, bandwidth_hz=None, nearby_hz=service.settings.nearby_hz)
        return _signals_with_list_ids(service, signals)

    @app.post("/api/signals")
    async def create_signal(body: SignalIn, service: AppService = Depends(get_service)):
        signal = Signal(**body.model_dump())
        service.create_signal(signal)
        return signal.model_dump(mode="json")

    @app.get("/api/signals/{signal_id}")
    async def get_signal(signal_id: str, service: AppService = Depends(get_service)):
        signal = service.signals.get(signal_id)
        if signal is None:
            raise HTTPException(404, "signal not found")
        return _signals_with_list_ids(service, [signal])[0]

    @app.patch("/api/signals/{signal_id}")
    async def patch_signal(
        signal_id: str, patch: SignalPatch, service: AppService = Depends(get_service)
    ):
        existing = service.signals.get(signal_id)
        if existing is None:
            raise HTTPException(404, "signal not found")
        data = patch.model_dump(exclude_unset=True)
        became_identified = data.get("status") == "identified" and existing.status != "identified"
        # model_validate (not model_copy) so a merged patch that would leave
        # the row invalid is rejected here rather than stored and only
        # surfacing later as a 500 out of signals.list()/state_payload().
        updated = Signal.model_validate(existing.model_dump() | data)
        service.update_signal(existing, updated)
        if became_identified:
            state = (
                service.states.get(existing.origin_state_id) if existing.origin_state_id else None
            )
            entry = service.journal.record_action("identified", state=state, signal_id=updated.id)
            await service.broadcaster.publish(
                {"type": "journal", "entry": entry.model_dump(mode="json")}
            )
        return updated.model_dump(mode="json")

    @app.delete("/api/signals/{signal_id}")
    async def delete_signal(signal_id: str, service: AppService = Depends(get_service)):
        signal = service.signals.get(signal_id)
        if signal is None:
            raise HTTPException(404, "signal not found")
        service.delete_signal(signal)
        return {"deleted": True}

    @app.get("/api/signals/{signal_id}/identify")
    async def identify_signal(signal_id: str, service: AppService = Depends(get_service)):
        signal = service.signals.get(signal_id)
        if signal is None:
            raise HTTPException(404, "signal not found")
        recipe = signal.recipe()
        q = IdentifyQuery(
            frequency_hz=recipe.frequency_hz,
            demodulator=recipe.demodulator,
            filter_bandwidth_hz=recipe.filter_bandwidth_hz,
            region=service.settings.region,
        )
        candidates = service.identify(q)
        return {"candidates": [c.model_dump(mode="json") for c in candidates]}

    # --- library (LIBRARY view: query, bulk ops, undo) -----------------------

    @app.get("/api/library")
    async def library_query(
        q: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        list: str | None = None,
        lo_hz: int | None = None,
        hi_hz: int | None = None,
        # None (not "created"): lets query() tell "not requested" apart from
        # an explicit "created", so a `list` filter with no `sort` defaults to
        # that list's own item order per the /api/library contract.
        sort: Literal["name", "frequency", "last_heard", "heard_count", "created"] | None = None,
        order: Literal["asc", "desc"] = "asc",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        service: AppService = Depends(get_service),
    ):
        items, total = service.signals.query(
            q=q,
            status=status,
            tag_id=tag,
            list_id=list,
            lo_hz=lo_hz,
            hi_hz=hi_hz,
            sort=sort,
            order=order,
            offset=offset,
            limit=limit,
        )
        return {"items": _signals_with_list_ids(service, items), "total": total}

    @app.post("/api/library/bulk")
    async def library_bulk(body: BulkOpIn, service: AppService = Depends(get_service)):
        result = service.bulk_library_op(body.ids, body.op, body.value)
        await service.broadcaster.publish({"type": "inbox"})
        return result

    @app.get("/api/library/duplicates")
    async def library_duplicates(
        tolerance_hz: int = 100, service: AppService = Depends(get_service)
    ):
        groups = service.find_duplicates(tolerance_hz)
        return {"groups": [[s.model_dump(mode="json") for s in g] for g in groups]}

    @app.post("/api/library/merge")
    async def library_merge(body: MergeIn, service: AppService = Depends(get_service)):
        signal = service.merge_signals(body.keep_id, body.merge_ids)
        await service.broadcaster.publish({"type": "inbox"})
        return signal.model_dump(mode="json")

    @app.get("/api/library/undo")
    async def library_undo_get(service: AppService = Depends(get_service)):
        return {"undo": service.library_undo.latest()}

    @app.post("/api/library/undo")
    async def library_undo_post(service: AppService = Depends(get_service)):
        if service.library_undo.latest() is None:
            raise HTTPException(409, "nothing to undo")
        label = service.library_undo.pop_and_restore(service.signals)
        await service.broadcaster.publish({"type": "inbox"})
        return {"undone": label, "undo": service.library_undo.latest()}

    @app.get("/api/library/redo")
    async def library_redo_get(service: AppService = Depends(get_service)):
        return {"redo": service.library_undo.latest_redo()}

    @app.post("/api/library/redo")
    async def library_redo_post(body: LibraryRedoIn, service: AppService = Depends(get_service)):
        try:
            label = service.library_undo.redo(service.signals, body.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await service.broadcaster.publish({"type": "inbox"})
        return {"redone": label, "redo": service.library_undo.latest_redo()}

    # --- lists (LIBRARY groupings) ------------------------------------------

    @app.get("/api/lists")
    async def get_lists(service: AppService = Depends(get_service)):
        return service.lists.all()

    @app.post("/api/lists")
    async def create_list(body: ListIn, service: AppService = Depends(get_service)):
        return service.create_list(body.name)

    @app.patch("/api/lists/{list_id}")
    async def patch_list(list_id: str, body: ListPatch, service: AppService = Depends(get_service)):
        data = body.model_dump(exclude_unset=True)
        return service.update_list(list_id, **data)

    @app.delete("/api/lists/{list_id}")
    async def delete_list(list_id: str, service: AppService = Depends(get_service)):
        service.delete_list(list_id)
        return {"deleted": True}

    @app.get("/api/lists/{list_id}/items")
    async def get_list_items(list_id: str, service: AppService = Depends(get_service)):
        return _signals_with_list_ids(service, service.list_items(list_id))

    @app.post("/api/lists/{list_id}/items")
    async def add_list_items(
        list_id: str, body: ListItemsIn, service: AppService = Depends(get_service)
    ):
        result = service.add_signals_to_list(list_id, body.ids)
        await service.broadcaster.publish({"type": "inbox"})
        return result

    # DELETE with a body is awkward across HTTP clients/servers, so removal
    # is a dedicated POST route rather than DELETE /api/lists/{id}/items.
    @app.post("/api/lists/{list_id}/items/remove")
    async def remove_list_items(
        list_id: str, body: ListItemsIn, service: AppService = Depends(get_service)
    ):
        result = service.remove_signals_from_list(list_id, body.ids)
        await service.broadcaster.publish({"type": "inbox"})
        return result

    @app.put("/api/lists/{list_id}/order")
    async def put_list_order(
        list_id: str, body: ListOrderIn, service: AppService = Depends(get_service)
    ):
        service.set_list_order(list_id, body.ids)
        await service.broadcaster.publish({"type": "inbox"})
        return {"ok": True}

    @app.patch("/api/lists/{list_id}/items/{signal_id}")
    async def patch_list_item(
        list_id: str,
        signal_id: str,
        body: ListItemSkipPatch,
        service: AppService = Depends(get_service),
    ):
        return service.set_list_item_skip(list_id, signal_id, body.skip)

    # --- saved searches (named LIBRARY queries) -----------------------------

    @app.get("/api/saved-searches")
    async def get_saved_searches(service: AppService = Depends(get_service)):
        return service.saved_searches.list()

    @app.post("/api/saved-searches")
    async def create_saved_search(body: SavedSearchIn, service: AppService = Depends(get_service)):
        return service.create_saved_search(body.name, body.params)

    @app.delete("/api/saved-searches/{saved_search_id}")
    async def delete_saved_search(saved_search_id: str, service: AppService = Depends(get_service)):
        service.saved_searches.delete(saved_search_id)
        return {"deleted": True}

    # --- identify ---------------------------------------------------------

    @app.get("/api/identify")
    async def identify(
        hz: int,
        mode: str | None = None,
        bw: int | None = None,
        service: AppService = Depends(get_service),
    ):
        q = IdentifyQuery(
            frequency_hz=hz,
            demodulator=mode,
            filter_bandwidth_hz=bw,
            region=service.settings.region,
        )
        candidates = service.identify(q)
        return {"candidates": [c.model_dump(mode="json") for c in candidates]}

    # --- satellite passes (nextpass) ---------------------------------------

    @app.get("/api/passes")
    async def get_passes(
        days: int | None = None,
        min_elevation: float | None = None,
        horizon: float | None = None,
        top: int | None = None,
        service: AppService = Depends(get_service),
    ):
        result = await service.passes(
            days=days, min_elevation=min_elevation, horizon=horizon, top=top
        )
        return result.model_dump(mode="json")

    @app.post("/api/passes/refresh")
    async def refresh_passes(days: int | None = None, service: AppService = Depends(get_service)):
        result = await service.passes(days=days, refresh=True)
        return result.model_dump(mode="json")

    @app.get("/api/passes.ics")
    async def get_passes_ics(service: AppService = Depends(get_service)):
        ics = await service.passes_ics()
        return PlainTextResponse(
            ics,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="passes.ics"'},
        )

    # --- sky (skyfield tracking) / Doppler tuning ---------------------------

    @app.get("/api/sky")
    async def get_sky(service: AppService = Depends(get_service)):
        return service.sky_summary()

    @app.post("/api/sky/refresh")
    async def refresh_sky(service: AppService = Depends(get_service)):
        return await service.refresh_sky()

    @app.get("/api/sky/{label}")
    async def get_sky_detail(
        label: str, hours: float = Query(default=24.0, gt=0, le=336),
        start: datetime | None = None, end: datetime | None = None,
        service: AppService = Depends(get_service),
    ):
        result = service.sky_detail(label, hours=hours, start=start, end=end)
        return result.model_dump(mode="json")

    @app.post("/api/track/{signal_id}")
    async def start_track(signal_id: str, service: AppService = Depends(get_service)):
        return await service.start_tracking(signal_id)

    @app.delete("/api/track")
    async def stop_track(service: AppService = Depends(get_service)):
        await service.stop_tracking()
        return {"stopped": True}

    # --- iqscan results (offline IQ-file scans) -----------------------------

    @app.get("/api/scans")
    async def list_scans(service: AppService = Depends(get_service)):
        if not service.settings.features.scans:
            raise HTTPException(404, "feature disabled: scans")
        scans = service.iqscan_provider.list_scans()
        return {
            "scans": [s.model_dump(mode="json") for s in scans],
            "root": str(service.iqscan_provider.root),
        }

    # Declared before /api/scans/{scan_id} so "jobs" isn't swallowed as a
    # scan_id (mirrors /api/signals/inbox and /api/signals/recent above).
    @app.get("/api/scans/jobs")
    async def scan_jobs(service: AppService = Depends(get_service)):
        return service.scan_jobs()

    @app.post("/api/scans/run")
    async def run_scan(body: ScanRunIn, service: AppService = Depends(get_service)):
        return service.run_scan(
            body.path,
            sample_rate=body.sample_rate,
            center_hz=body.center_hz,
            threshold=body.threshold,
            format=body.format,
            channel_clips=body.channel_clips,
            redetect=body.redetect,
        )

    @app.get("/api/scans/{scan_id}")
    async def get_scan(scan_id: str, service: AppService = Depends(get_service)):
        if not service.settings.features.scans:
            raise HTTPException(404, "feature disabled: scans")
        scans = service.iqscan_provider.list_scans()
        scan = next((s for s in scans if s.id == scan_id), None)
        if scan is None:
            raise HTTPException(404, "scan not found")
        events = service.iqscan_provider.events(scan_id)
        return {
            "scan": scan.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in events],
        }

    @app.get("/api/scans/{scan_id}/report/{name}")
    async def scan_report(scan_id: str, name: str, service: AppService = Depends(get_service)):
        if not service.settings.features.scans:
            raise HTTPException(404, "feature disabled: scans")
        path = service.iqscan_provider.report_path(scan_id, name)
        if path is None:
            raise HTTPException(404, "report not found")
        return FileResponse(path)

    @app.get("/api/scans/{scan_id}/clips/{name}")
    async def scan_clip(scan_id: str, name: str, service: AppService = Depends(get_service)):
        if not service.settings.features.scans:
            raise HTTPException(404, "feature disabled: scans")
        path = service.iqscan_provider.clip_path(scan_id, name)
        if path is None:
            raise HTTPException(404, "clip not found")
        return FileResponse(path, media_type="application/octet-stream")

    # --- scanner (v0.12.0: sweep a list or a frequency range) ---------------

    @app.post("/api/scan/start")
    async def scan_start(body: ScanStartIn, service: AppService = Depends(get_service)):
        return await service.scan_start(body)

    @app.post("/api/scan/pause")
    async def scan_pause(service: AppService = Depends(get_service)):
        return await service.scan_pause()

    @app.post("/api/scan/resume")
    async def scan_resume(service: AppService = Depends(get_service)):
        return await service.scan_resume()

    @app.post("/api/scan/stop")
    async def scan_stop(service: AppService = Depends(get_service)):
        return await service.scan_stop()

    @app.post("/api/scan/skip")
    async def scan_skip(body: ScanSkipIn, service: AppService = Depends(get_service)):
        return service.scan_skip(body.persist)

    @app.post("/api/scan/hold")
    async def scan_hold(service: AppService = Depends(get_service)):
        return service.scan_hold()

    @app.get("/api/scan")
    async def scan_status(service: AppService = Depends(get_service)):
        return service.scan_status()

    # --- receiver owner (U1) -------------------------------------------------

    @app.post("/api/owner/stop")
    async def owner_stop(service: AppService = Depends(get_service)):
        return await service.owner_stop()

    @app.get("/api/owner/conflicts")
    async def owner_conflicts(
        action: Literal[
            "scan", "track", "beacons", "survey_span", "band_decode", "analyzer", "record"
        ],
        service: AppService = Depends(get_service),
    ):
        return service.owner_conflicts(action)

    # --- pre-roll IQ recorder ------------------------------------------------

    @app.get("/api/recorder")
    async def get_recorder(service: AppService = Depends(get_service)):
        return service.recorder_status()

    @app.post("/api/recorder/arm")
    async def recorder_arm(body: RecorderArmIn, service: AppService = Depends(get_service)):
        return await service.recorder_arm(body.on)

    @app.post("/api/recorder/start")
    async def recorder_start(service: AppService = Depends(get_service)):
        return await service.recorder_start()

    @app.post("/api/recorder/stop")
    async def recorder_stop(service: AppService = Depends(get_service)):
        return await service.recorder_stop()

    @app.get("/api/recorder/recovery")
    async def recorder_recovery(service: AppService = Depends(get_service)):
        service._require_recorder_feature()
        return service.recorder.recoverable_recordings()

    @app.post("/api/recorder/recovery/{name}")
    async def recorder_repair(name: str, service: AppService = Depends(get_service)):
        service._require_recorder_feature()
        return await service.recorder.repair_recording(name)

    # --- recordings (U1: browse/manage finished SigMF recordings) -----------

    @app.get("/api/recordings")
    async def list_recordings(service: AppService = Depends(get_service)):
        return service.recordings_list()

    @app.delete("/api/recordings/{name}")
    async def delete_recording(name: str, service: AppService = Depends(get_service)):
        return service.recordings_delete(name)

    @app.post("/api/recordings/{name}/reveal")
    async def reveal_recording(name: str, service: AppService = Depends(get_service)):
        return service.recordings_reveal(name)

    @app.post("/api/recordings/{name}/analyze")
    async def analyze_recording(name: str, service: AppService = Depends(get_service)):
        return service.recordings_analyze(name)

    # --- wifi (passive Wi-Fi reception, docs/wifi.md) ------------------------

    @app.get("/api/wifi/status")
    async def wifi_status(service: AppService = Depends(get_service)):
        return service.wifi_status()

    @app.get("/api/wifi/summary")
    async def wifi_summary(
        window_s: float = Query(default=60.0, ge=10, le=3600),
        service: AppService = Depends(get_service),
    ):
        return service.wifi_network_summary(window_s)

    @app.get("/api/wifi/vendors")
    async def wifi_vendors(service: AppService = Depends(get_service)):
        return service.wifi_vendors_status()

    @app.post("/api/wifi/vendors/refresh")
    async def wifi_vendors_refresh(service: AppService = Depends(get_service)):
        return await service.wifi_vendors_refresh()

    @app.get("/api/wifi/devices")
    async def wifi_devices(service: AppService = Depends(get_service)):
        return await service.wifi_devices()

    @app.post("/api/wifi/start")
    async def wifi_start(body: WifiStartIn, service: AppService = Depends(get_service)):
        return await service.wifi_start(body.channel)

    @app.post("/api/wifi/stop")
    async def wifi_stop(service: AppService = Depends(get_service)):
        return await service.wifi_stop()

    @app.get("/api/wifi/networks")
    async def wifi_networks(
        source: str = "live",
        q: str | None = None,
        channel: int | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        sort: str = Query(
            default="first_seen",
            pattern="^(ssid|bssid|channel|security|signal|first_seen|last_seen|packets)$",
        ),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
        service: AppService = Depends(get_service),
    ):
        return service.wifi_networks(
            source=source, q=q, channel=channel, offset=offset, limit=limit,
            sort=sort, order=order,
        )

    @app.post("/api/wifi/networks/clear")
    async def wifi_clear_networks(service: AppService = Depends(get_service)):
        return service.wifi_clear_networks()

    @app.get("/api/wifi/live")
    async def wifi_live(
        after: int = Query(default=0, ge=0),
        kind: str = Query(default="all", pattern="^(all|management|control|data)$"),
        limit: int = Query(default=200, ge=1, le=500),
        service: AppService = Depends(get_service),
    ):
        return service.wifi_live(after=after, kind=kind, limit=limit)

    @app.get("/api/wifi/networks/{bssid}")
    async def wifi_network_details(
        bssid: str, source: str = "live", service: AppService = Depends(get_service)
    ):
        return service.wifi_network_details(bssid, source=source)

    @app.get("/api/wifi/networks/{bssid}/frames/{n}")
    async def wifi_network_frame(
        bssid: str, n: int, source: str = "live", service: AppService = Depends(get_service)
    ):
        return {"hex": service.wifi_network_frame(bssid, n, source=source)}

    @app.post("/api/wifi/sessions/start")
    async def wifi_session_start(service: AppService = Depends(get_service)):
        return await service.wifi_session_start()

    @app.post("/api/wifi/sessions/stop")
    async def wifi_session_stop(service: AppService = Depends(get_service)):
        return await service.wifi_session_stop()

    @app.get("/api/wifi/sessions")
    async def wifi_sessions_list(service: AppService = Depends(get_service)):
        return service.wifi_sessions_list()

    @app.get("/api/wifi/sessions/{session_id}/export.pcap")
    async def wifi_session_export(session_id: str, service: AppService = Depends(get_service)):
        path = service.wifi_session_export_path(session_id)
        return FileResponse(
            path, media_type="application/vnd.tcpdump.pcap", filename=f"{session_id}.pcap"
        )

    @app.get("/api/wifi/sessions/{session_id}/remoteid")
    async def wifi_session_remoteid(session_id: str, service: AppService = Depends(get_service)):
        return service.wifi_session_remoteid(session_id)

    @app.post("/api/wifi/import")
    async def wifi_import(request: Request, service: AppService = Depends(get_service)):
        # Raw pcap bytes as the request body (not multipart/UploadFile --
        # that needs python-multipart, an otherwise-unused dependency).
        service._require_wifi_action_enabled()
        size = 0
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pcap")
        tmp_path = Path(tmp.name)
        # One finally covers the upload as well: a client that disconnects
        # mid-stream must not leave a partial (up to the cap) file behind.
        try:
            with tmp:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > WIFI_IMPORT_MAX_BYTES:
                        raise HTTPException(
                            413,
                            f"file too large (max {WIFI_IMPORT_MAX_BYTES // (1024 * 1024)} MB)",
                        )
                    tmp.write(chunk)
            if size < 24 or tmp_path.read_bytes()[:4] not in _PCAP_MAGICS:
                raise HTTPException(400, "not a pcap file")
            return await service.wifi_import(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/wifi/survey/start")
    async def wifi_survey_start(body: WifiSurveyIn, service: AppService = Depends(get_service)):
        return await service.wifi_survey_start(body.channels, body.dwell_s, body.rounds)

    @app.post("/api/wifi/survey/stop")
    async def wifi_survey_stop(service: AppService = Depends(get_service)):
        return await service.wifi_survey_stop()

    @app.get("/api/wifi/survey")
    async def wifi_survey_status(service: AppService = Depends(get_service)):
        return service.wifi_survey_status()

    @app.get("/api/wifi/activity")
    async def wifi_activity(
        minutes: float = Query(default=30, gt=0, le=1440),
        bucket: int | None = Query(default=None),
        service: AppService = Depends(get_service),
    ):
        if bucket is not None and bucket not in (1, 5, 15, 60, 300):
            raise HTTPException(422, "bucket must be 1, 5, 15, 60 or 300")
        return service.wifi_activity(minutes=minutes, bucket=bucket)

    # --- history, timeline and labels (docs/history.md) ----------------------

    @app.get("/api/history/status")
    async def history_status(service: AppService = Depends(get_service)):
        return service.history_status()

    @app.post("/api/history/clear")
    async def history_clear(service: AppService = Depends(get_service)):
        return service.history_clear()

    @app.get("/api/history/timeline")
    async def history_timeline(
        hours: float | None = Query(default=None, ge=0.25, le=168),
        start: float | None = Query(default=None),
        end: float | None = Query(default=None),
        receivers: str = Query(default="wifi,ble", pattern=r"^(wifi|ble)(,(wifi|ble))?$"),
        include_private: bool = False,
        hide: str = Query(default="", pattern=r"^((mine|known|ignore)(,(mine|known|ignore))*)?$"),
        q: str | None = Query(default=None, max_length=80),
        limit: int = Query(default=200, ge=1, le=500),
        bucket: int | None = Query(default=None),
        service: AppService = Depends(get_service),
    ):
        if bucket is not None and bucket not in (60, 300, 900, 3600):
            raise HTTPException(422, "bucket must be 60, 300, 900 or 3600")
        return service.history_timeline(
            hours=hours,
            start=start,
            end=end,
            receivers=tuple(dict.fromkeys(receivers.split(","))),
            include_private=include_private,
            hide={c for c in hide.split(",") if c},
            q=q or None,
            limit=limit,
            bucket=bucket,
        )

    @app.get("/api/history/compare")
    async def history_compare(
        a_start: float = Query(...),
        a_end: float = Query(...),
        b_start: float = Query(...),
        b_end: float = Query(...),
        receivers: str = Query(default="wifi,ble", pattern=r"^(wifi|ble)(,(wifi|ble))?$"),
        include_private: bool = False,
        service: AppService = Depends(get_service),
    ):
        return service.history_compare(
            a_start=a_start,
            a_end=a_end,
            b_start=b_start,
            b_end=b_end,
            receivers=tuple(dict.fromkeys(receivers.split(","))),
            include_private=include_private,
        )

    @app.get("/api/history/device/{receiver}/{ident}")
    async def history_device(
        receiver: str,
        ident: str,
        hours: float = Query(default=24, ge=0.25, le=168),
        bucket: int | None = Query(default=None),
        service: AppService = Depends(get_service),
    ):
        if bucket is not None and bucket not in (60, 300, 900, 3600):
            raise HTTPException(422, "bucket must be 60, 300, 900 or 3600")
        return service.history_device(receiver, ident, hours=hours, bucket=bucket)

    @app.get("/api/labels")
    async def labels_list(
        receiver: str | None = None, service: AppService = Depends(get_service)
    ):
        return service.labels_list(receiver)

    @app.put("/api/labels/{receiver}/{ident}")
    async def label_put(
        receiver: str, ident: str, body: LabelIn, service: AppService = Depends(get_service)
    ):
        return service.label_put(receiver, ident, name=body.name, note=body.note, cls=body.cls)

    @app.delete("/api/labels/{receiver}/{ident}")
    async def label_delete(receiver: str, ident: str, service: AppService = Depends(get_service)):
        return service.label_delete(receiver, ident)

    # --- ble (passive BLE reception, docs/ble.md) ----------------------------

    @app.get("/api/ble/status")
    async def ble_status(service: AppService = Depends(get_service)):
        return service.ble_status()

    @app.get("/api/ble/devices")
    async def ble_devices(service: AppService = Depends(get_service)):
        return await service.ble_devices()

    @app.get("/api/ble/summary")
    async def ble_summary(
        window_s: float = Query(default=60.0, ge=10, le=3600),
        service: AppService = Depends(get_service),
    ):
        return service.ble_device_summary(window_s)

    @app.get("/api/ble/names")
    async def ble_names(service: AppService = Depends(get_service)):
        return service.ble_names_status()

    @app.post("/api/ble/names/refresh")
    async def ble_names_refresh(service: AppService = Depends(get_service)):
        return await service.ble_names_refresh()

    @app.post("/api/ble/start")
    async def ble_start(service: AppService = Depends(get_service)):
        return await service.ble_start()

    @app.post("/api/ble/stop")
    async def ble_stop(service: AppService = Depends(get_service)):
        return await service.ble_stop()

    @app.get("/api/ble/advertisers")
    async def ble_advertisers(
        q: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        sort: str = Query(
            default="first_seen",
            pattern="^(first_seen|last_seen|addr|name|rssi|reports|company|identity|services|device|sensor)$",
        ),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
        service: AppService = Depends(get_service),
    ):
        return service.ble_advertisers(q=q, offset=offset, limit=limit, sort=sort, order=order)

    @app.post("/api/ble/advertisers/clear")
    async def ble_clear_advertisers(service: AppService = Depends(get_service)):
        return service.ble_clear_advertisers()

    @app.get("/api/ble/live")
    async def ble_live(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
        service: AppService = Depends(get_service),
    ):
        return service.ble_live(after=after, limit=limit)

    @app.get("/api/ble/advertisers/{addr}")
    async def ble_advertiser_details(addr: str, service: AppService = Depends(get_service)):
        return service.ble_advertiser_details(addr)

    # --- remote id (live drone Remote ID, docs/remote-id.md) -----------------

    @app.get("/api/remoteid/live")
    async def remoteid_live(service: AppService = Depends(get_service)):
        return service.remoteid_live()

    @app.get("/api/remoteid/uas/{key}")
    async def remoteid_uas_details(key: str, service: AppService = Depends(get_service)):
        return service.remoteid_uas_details(key)

    # --- watch rules (docs/rules.md) ------------------------------------------

    @app.get("/api/rules")
    async def rules_list(service: AppService = Depends(get_service)):
        return service.rules_list()

    @app.post("/api/rules")
    async def rules_create(body: RuleIn, service: AppService = Depends(get_service)):
        return service.rules_create(
            kind=body.kind, name=body.name, config=body.config,
            enabled=body.enabled, cooldown_s=body.cooldown_s,
        )

    @app.patch("/api/rules/{rule_id}")
    async def rules_update(
        rule_id: int, body: RulePatch, service: AppService = Depends(get_service)
    ):
        return service.rules_update(
            rule_id, name=body.name, config=body.config,
            enabled=body.enabled, cooldown_s=body.cooldown_s,
        )

    @app.delete("/api/rules/{rule_id}")
    async def rules_delete(rule_id: int, service: AppService = Depends(get_service)):
        return service.rules_delete(rule_id)

    @app.get("/api/rules/events")
    async def rules_events(
        rule_id: int | None = None,
        limit: int = Query(default=200, ge=1, le=2000),
        service: AppService = Depends(get_service),
    ):
        return service.rules_events(rule_id=rule_id, limit=limit)

    @app.post("/api/rules/{rule_id}/test")
    async def rules_test(rule_id: int, service: AppService = Depends(get_service)):
        return service.rules_test(rule_id)

    # --- signal decoders (P1) ------------------------------------------------

    @app.get("/api/validation")
    async def get_validation(service: AppService = Depends(get_service)):
        # Pure read of committed manifest/matrix/scorecard files: never runs a
        # decoder or touches the radio. Always lists the outstanding gates so
        # a green-looking UI can't hide HF field validation (plan S5).
        from msdr.validation.status import validation_status

        return await asyncio.to_thread(validation_status, service.settings)

    @app.get("/api/decoders")
    async def list_decoders(service: AppService = Depends(get_service)):
        # First call may probe decoder binaries for their version (2 s cap
        # each, cached after) -- keep that off the event loop.
        return await asyncio.to_thread(service.list_decoders)

    @app.get("/api/decoder")
    async def get_decoder(service: AppService = Depends(get_service)):
        return service.decoder_status()

    @app.post("/api/decoder/prepare")
    async def decoder_prepare(body: DecoderPrepareIn, service: AppService = Depends(get_service)):
        try:
            return await service.decoder_prepare(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/decoder/restore")
    async def decoder_restore(body: DecoderRestoreIn, service: AppService = Depends(get_service)):
        try:
            return await service.decoder_restore(body.token)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/decoder/reapply")
    async def decoder_reapply(body: DecoderRestoreIn, service: AppService = Depends(get_service)):
        try:
            return await service.decoder_reapply(body.token)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/decoder/start")
    async def decoder_start(body: DecoderStartIn, service: AppService = Depends(get_service)):
        return await service.decoder_start(body.decoders, body.frequency_hz)

    @app.post("/api/decoder/stop")
    async def decoder_stop(service: AppService = Depends(get_service)):
        return await service.decoder_stop()

    @app.get("/api/decoder/events")
    async def decoder_events(
        limit: int = 200,
        decoder: str | None = None,
        q: str | None = None,
        since: float | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.decoder_events(limit=limit, decoder=decoder, q=q, since=since)

    @app.delete("/api/decoder/events")
    async def decoder_clear(service: AppService = Depends(get_service)):
        return {"cleared": service.decoder_clear()}

    @app.get("/api/decoder/calls/{event_id}.wav")
    async def decoder_call_wav(event_id: str, service: AppService = Depends(get_service)):
        path = service.decoder_call_wav_path(event_id)
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/decoder/images/{event_id}.png")
    async def decoder_image(event_id: str, service: AppService = Depends(get_service)):
        path = service.decoder_image_path(event_id)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/decoder/presets/install")
    async def decoder_presets_install(service: AppService = Depends(get_service)):
        return service.decoder_presets_install()

    @app.post("/api/decoder/band/start")
    async def decoder_band_start(body: BandStartIn, service: AppService = Depends(get_service)):
        return await service.decoder_band_start(
            body.decoders, max_channels=body.max_channels, threshold_db=body.threshold_db
        )

    @app.post("/api/decoder/band/stop")
    async def decoder_band_stop(service: AppService = Depends(get_service)):
        return await service.decoder_band_stop()

    @app.post("/api/decoder/band/save")
    async def decoder_band_save(body: BandSaveIn, service: AppService = Depends(get_service)):
        return service.decoder_band_save(body.frequency_hz, body.name)

    # --- wide-view survey (S1) ------------------------------------------------

    @app.get("/api/survey")
    async def get_survey(service: AppService = Depends(get_service)):
        return service.survey_status()

    @app.post("/api/survey/start")
    async def survey_start(service: AppService = Depends(get_service)):
        return await service.survey_start()

    @app.get("/api/survey/band-scan")
    async def survey_band_scan_status(service: AppService = Depends(get_service)):
        return service.survey_band_scan_status()

    @app.delete("/api/survey/band-scan")
    async def delete_all_band_scan_reports(service: AppService = Depends(get_service)):
        return await service.survey_band_scan_delete()

    @app.delete("/api/survey/band-scan/{scan_id}")
    async def delete_band_scan_report(scan_id: str, service: AppService = Depends(get_service)):
        return await service.survey_band_scan_delete(scan_id)

    @app.get("/api/survey/band-scan/compare")
    async def compare_band_scans(
        before: str, after: str,
        tolerance_hz: float = Query(default=2000, ge=0, le=100_000),
        service: AppService = Depends(get_service),
    ):
        from msdr.survey.compare import compare_scans

        try:
            first, second = await asyncio.gather(
                service.survey_band_scan_read(before, "summary"),
                service.survey_band_scan_read(after, "summary"),
            )
            return await asyncio.to_thread(compare_scans, first, second, tolerance_hz)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/survey/band-scan/{scan_id}")
    async def survey_band_scan_get(scan_id: str, service: AppService = Depends(get_service)):
        data = await service.survey_band_scan_read(scan_id, "export_bytes")
        return Response(data, media_type="application/json")

    @app.get("/api/survey/band-scan/{scan_id}/summary")
    async def survey_band_scan_summary(scan_id: str, service: AppService = Depends(get_service)):
        detail = await service.survey_band_scan_read(scan_id, "summary")
        return await asyncio.to_thread(JSONResponse, detail)

    @app.get("/api/survey/band-scan/{scan_id}/observations")
    async def survey_band_scan_observations(
        scan_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        frequency_hz: float | None = Query(default=None, ge=0),
        min_peak_snr_db: float | None = Query(default=None, allow_inf_nan=False),
        service: AppService = Depends(get_service),
    ):
        detail = await service.survey_band_scan_read(
            scan_id, "observations", offset=offset, limit=limit, frequency_hz=frequency_hz,
            min_peak_snr_db=min_peak_snr_db,
        )
        return await asyncio.to_thread(JSONResponse, detail)

    @app.get("/api/survey/band-scan/{scan_id}/spectrum")
    async def survey_band_scan_spectrum(scan_id: str, service: AppService = Depends(get_service)):
        detail = await service.survey_band_scan_read(scan_id, "spectrum")
        return await asyncio.to_thread(JSONResponse, detail)

    @app.get("/api/survey/band-scan/{scan_id}/export")
    async def survey_band_scan_export(scan_id: str, service: AppService = Depends(get_service)):
        data = await service.survey_band_scan_read(scan_id, "export_bytes")
        return Response(
            data, media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="band-scan.json"'},
        )

    @app.post("/api/survey/band-scan/start")
    async def survey_band_scan_start(
        body: SurveyBandScanStartIn, service: AppService = Depends(get_service)
    ):
        return await service.survey_band_scan_start(body.model_dump())

    @app.post("/api/survey/band-scan/pause")
    async def survey_band_scan_pause(service: AppService = Depends(get_service)):
        return service.survey_band_scan_pause()

    @app.post("/api/survey/band-scan/resume")
    async def survey_band_scan_resume(service: AppService = Depends(get_service)):
        return service.survey_band_scan_resume()

    @app.post("/api/survey/band-scan/stop")
    async def survey_band_scan_stop(service: AppService = Depends(get_service)):
        return await service.survey_band_scan_stop()

    @app.post("/api/survey/stop")
    async def survey_stop(service: AppService = Depends(get_service)):
        return await service.survey_stop()

    @app.post("/api/survey/clear")
    async def survey_clear(service: AppService = Depends(get_service)):
        return await service.survey_clear()

    @app.post("/api/survey/replay")
    async def survey_replay(body: SurveyReplayIn, service: AppService = Depends(get_service)):
        return await service.survey_replay(body.path, body.loop)

    @app.post("/api/survey/replay/stop")
    async def survey_replay_stop(service: AppService = Depends(get_service)):
        return await service.survey_replay_stop()

    @app.post("/api/survey/replay/control")
    async def survey_replay_control(
        body: SurveyReplayControlIn, service: AppService = Depends(get_service)
    ):
        return await service.survey_replay_control(body.playing, body.position_s, body.loop)

    @app.post("/api/survey/span")
    async def survey_span(body: SurveySpanIn, service: AppService = Depends(get_service)):
        return await service.survey_span(body.sample_rate_hz, body.center_hz, body.keep_vfo)

    @app.post("/api/survey/dimensions")
    async def survey_dimensions(
        body: SurveyDimensionsIn, service: AppService = Depends(get_service)
    ):
        return service.survey_dimensions(body.dimensions)

    @app.post("/api/survey/decode")
    async def survey_decode(body: SurveyDecodeIn, service: AppService = Depends(get_service)):
        return await service.survey_decode(body.frequency_hz, body.decoders)

    @app.post("/api/survey/test-signal")
    async def survey_test_signal(
        body: SurveyTestSignalIn, service: AppService = Depends(get_service)
    ):
        return await service.survey_test_signal(body.frequency_hz, body.decoders)

    @app.post("/api/survey/optimize-decode")
    async def survey_optimize_decode(
        body: SurveyTestSignalIn, service: AppService = Depends(get_service)
    ):
        return await service.survey_test_signal(body.frequency_hz, body.decoders, optimize=True)

    @app.post("/api/survey/undecode")
    async def survey_undecode(body: SurveyUndecodeIn, service: AppService = Depends(get_service)):
        return await service.survey_undecode(body.frequency_hz)

    @app.post("/api/survey/save")
    async def survey_save(body: SurveySaveIn, service: AppService = Depends(get_service)):
        return service.survey_save(body.frequency_hz, body.name)

    # --- signal workbench: explicit live inspection and activity watching ---
    @app.get("/api/workbench")
    async def workbench_status(service: AppService = Depends(get_service)):
        return service.workbench.status()

    @app.post("/api/workbench/watch")
    async def workbench_watch(body: WatchConfig, service: AppService = Depends(get_service)):
        try:
            result = await service.workbench.start_watch(body)
            await service._publish_owner()
            return result
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/workbench/watch/stop")
    async def workbench_watch_stop(service: AppService = Depends(get_service)):
        # Stop only the channel watch (and its capture trigger); a live
        # inspection the operator started separately keeps running.
        await service.workbench.stop_watch()
        await service._publish_owner()
        return service.workbench.status()

    @app.post("/api/workbench/stop")
    async def workbench_stop(service: AppService = Depends(get_service)):
        await service.workbench.stop()
        await service._publish_owner()
        return service.workbench.status()

    @app.post("/api/inspection/live/start")
    async def inspection_live_start(
        body: LiveInspectionConfig, service: AppService = Depends(get_service),
    ):
        try:
            return await service.workbench.start_live(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/inspection/live/stop")
    async def inspection_live_stop(service: AppService = Depends(get_service)):
        await service.workbench.stop_live()
        return {"active": False}

    @app.get("/api/inspection/{mode}")
    async def inspection_snapshot(
        mode: Literal["review", "live"],
        stream: Literal["all", "audio", "iq_envelope", "iq_i", "iq_q", "audio_spectrum"] = "all",
        window_s: float = Query(default=0.1, ge=0.001, le=5),
        points: int = Query(default=512, ge=16, le=2048),
        service: AppService = Depends(get_service),
    ):
        try:
            rx = await service.workbench.inspection_receiver(mode)
            snapshot = rx.inspection.snapshot(stream, window_s=window_s, points=points)
            return {**snapshot, "mode": mode, "receiver": rx.status(),
                    "drift": rx.drift_status(), "units": "normalized amplitude; uncalibrated"}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/inspection/{mode}/configure")
    async def inspection_configure(
        mode: Literal["review", "live"], body: ScopeConfig,
        service: AppService = Depends(get_service),
    ):
        try:
            rx = await service.workbench.inspection_receiver(mode)
            rx.inspection.configure(**body.model_dump())
            return {"configured": True}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/inspection/{mode}/drift")
    async def inspection_drift(
        mode: Literal["review", "live"], body: DriftConfig,
        service: AppService = Depends(get_service),
    ):
        try:
            rx = await service.workbench.inspection_receiver(mode)
            return rx.configure_drift(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # --- review: source-independent playback + audio (S3) --------------------
    # Every route below acts on the replay's VirtualReceiver only -- never
    # self.adapter/the live radio (see AppService's review_* methods).

    @app.get("/api/review")
    async def get_review(service: AppService = Depends(get_service)):
        return service.review_status()

    @app.post("/api/review/tune")
    async def review_tune(body: ReviewTuneIn, service: AppService = Depends(get_service)):
        return service.review_tune(body.frequency_hz, body.demodulator, body.bandwidth_hz)

    @app.post("/api/review/audio")
    async def review_audio(body: ReviewAudioIn, service: AppService = Depends(get_service)):
        return service.review_audio(squelch_db=body.squelch_db, volume=body.volume, mute=body.mute)

    @app.post("/api/receiver/audio-dsp")
    async def receiver_audio_dsp(
        body: ReceiverAudioDspIn, service: AppService = Depends(get_service)
    ):
        try:
            return await service.receiver_audio_dsp(body.property, body.value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/inspection/{mode}/audio-dsp")
    async def inspection_audio_dsp(
        mode: Literal["review", "live"],
        body: NativeAudioPatch,
        service: AppService = Depends(get_service),
    ):
        try:
            return await service.configure_native_audio(mode, body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/review/seek")
    async def review_seek(body: ReviewSeekIn, service: AppService = Depends(get_service)):
        return service.review_seek(body.position_s)

    @app.post("/api/review/loop")
    async def review_loop(body: ReviewLoopIn, service: AppService = Depends(get_service)):
        return service.review_loop(body.loop)

    @app.post("/api/review/bookmarks")
    async def review_bookmark_create(body: BookmarkIn, service: AppService = Depends(get_service)):
        return service.review_bookmark_create(body.note)

    @app.get("/api/review/bookmarks")
    async def review_bookmarks_list(path: str, service: AppService = Depends(get_service)):
        return service.review_bookmarks_list(path)

    @app.post("/api/review/bookmarks/{bookmark_id}/seek")
    async def review_bookmark_seek(bookmark_id: str, service: AppService = Depends(get_service)):
        return service.review_bookmark_seek(bookmark_id)

    @app.post("/api/review/clips")
    async def review_clip_create(body: ClipIn, service: AppService = Depends(get_service)):
        return await service.review_clip_create(body.start_s, body.end_s, body.note)

    @app.get("/api/review/clips")
    async def review_clips_list(path: str, service: AppService = Depends(get_service)):
        return service.review_clips_list(path)

    @app.post("/api/review/decode")
    async def review_decode_start(body: ReviewDecodeIn, service: AppService = Depends(get_service)):
        return await service.review_decode_start(
            body.decoders,
            body.mode,
            max_channels=body.max_channels,
            threshold_db=body.threshold_db,
        )

    @app.post("/api/review/decode/stop")
    async def review_decode_stop(service: AppService = Depends(get_service)):
        return await service.review_decode_stop()

    # --- beacon monitor (S10) ------------------------------------------------

    @app.get("/api/beacons")
    async def get_beacons(service: AppService = Depends(get_service)):
        return service.beacons_status()

    @app.post("/api/beacons/start")
    async def beacons_start(body: BeaconStartIn, service: AppService = Depends(get_service)):
        return await service.beacons_start(body.mode, body.frequencies_hz)

    @app.post("/api/beacons/stop")
    async def beacons_stop(service: AppService = Depends(get_service)):
        return await service.beacons_stop()

    @app.get("/api/beacons/hits")
    async def get_beacon_hits(
        since: float | None = None,
        limit: int = 200,
        beacon: str | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.beacon_hits_list(limit=limit, since=since, beacon=beacon)

    @app.delete("/api/beacons/hits")
    async def delete_beacon_hits(service: AppService = Depends(get_service)):
        return {"cleared": service.beacon_hits_clear()}

    # --- scanner finds log (S11) ---------------------------------------------

    @app.get("/api/finds")
    async def get_finds(
        status: str | None = None,
        limit: int = 200,
        q: str | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.finds_list(status=status, limit=limit, q=q)

    @app.patch("/api/finds/{find_id}")
    async def patch_find(
        find_id: str, body: FindPatchIn, service: AppService = Depends(get_service)
    ):
        return service.finds_patch(find_id, status=body.status, note=body.note)

    @app.delete("/api/finds/{find_id}")
    async def delete_find(find_id: str, service: AppService = Depends(get_service)):
        return service.finds_delete(find_id)

    @app.post("/api/finds/{find_id}/promote")
    async def promote_find(
        find_id: str, body: FindPromoteIn, service: AppService = Depends(get_service)
    ):
        return service.finds_promote(find_id, name=body.name)

    @app.post("/api/finds/{find_id}/tune")
    async def tune_find(find_id: str, service: AppService = Depends(get_service)):
        return restore_report_to_dict(await service.finds_tune(find_id))

    # --- signal detail panel (U3) -------------------------------------------

    @app.get("/api/signal-detail")
    async def get_signal_detail(
        frequency_hz: int | None = None,
        signal_id: str | None = None,
        find_id: str | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.signal_detail(
            frequency_hz=frequency_hz, signal_id=signal_id, find_id=find_id
        )

    # --- observations and honest evidence (S2) ------------------------------

    @app.get("/api/observations")
    async def get_observations(
        frequency_hz: int | None = None,
        tolerance_hz: int = 6000,
        signal_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        service: AppService = Depends(get_service),
    ):
        return service.observations_list(
            frequency_hz=frequency_hz,
            tolerance_hz=tolerance_hz,
            signal_id=signal_id,
            session_id=session_id,
            limit=limit,
        )

    # Before /{observation_id} so "export" isn't taken for an id.
    @app.get("/api/observations/export")
    async def export_observations(
        since_ts: float | None = None,
        limit: int = 500,
        service: AppService = Depends(get_service),
    ):
        return service.observations_export(since_ts=since_ts, limit=limit)

    @app.get("/api/observations/{observation_id}/export")
    async def export_observation(observation_id: str, service: AppService = Depends(get_service)):
        return service.observation_export(observation_id)

    @app.post("/api/observations/{observation_id}/promote")
    async def promote_observation(
        observation_id: str,
        body: ObservationPromoteIn | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.observation_promote(
            observation_id, name=body.name if body is not None else None
        )

    @app.get("/api/observations/{observation_id}")
    async def get_observation(observation_id: str, service: AppService = Depends(get_service)):
        return service.observation_get(observation_id)

    @app.patch("/api/observations/{observation_id}")
    async def patch_observation(
        observation_id: str, body: ObservationPatch, service: AppService = Depends(get_service)
    ):
        return service.observation_patch(
            observation_id,
            signal_id=body.signal_id,
            notes=body.notes,
            fields=body.model_fields_set,
        )

    @app.post("/api/observations/{observation_id}/claims")
    async def post_observation_claim(
        observation_id: str, body: OperatorClaimIn, service: AppService = Depends(get_service)
    ):
        return service.observation_add_claim(
            observation_id,
            subject=body.subject,
            value=body.value,
            rationale=body.rationale,
            supersedes=body.supersedes,
        )

    # --- catalog (searchable frequency catalog) -----------------------------

    @app.get("/api/catalog/search")
    async def catalog_search(
        q: str = "",
        on_air: bool = False,
        lo_hz: int | None = None,
        hi_hz: int | None = None,
        source: str | None = None,
        sources: list[str] = Query(default=[]),
        languages: list[str] = Query(default=[]),
        has_coordinates: bool | None = None,
        network: str | None = None,
        station_id: str | None = None,
        ref_type: str | None = None,
        sort: Literal["relevance", "frequency", "distance", "time", "name"] = "relevance",
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        service: AppService = Depends(get_service),
    ):
        # Older cached clients serialized empty arrays as `sources=` and
        # `languages=`. Treat those blank repeated values as no filter.
        sources = [value.strip() for value in sources if value.strip()]
        languages = [value.strip() for value in languages if value.strip()]
        source = source.strip() if source and source.strip() else None
        network = network.strip() if network and network.strip() else None
        station_id = station_id.strip() if station_id and station_id.strip() else None
        ref_type = ref_type.strip() if ref_type and ref_type.strip() else None
        band = (lo_hz, hi_hz) if lo_hz is not None or hi_hz is not None else None
        entries, count = service.catalog_search(
            q,
            on_air=on_air,
            band=band,
            source=source,
            sources=sources,
            languages=languages,
            limit=limit,
            offset=offset,
            has_coordinates=has_coordinates,
            network=network,
            station_id=station_id,
            ref_type=ref_type,
            sort=sort,
        )
        return {
            "entries": service.catalog_entry_dicts(entries, with_distance=sort == "distance"),
            "count": count,
            "offset": offset,
            "limit": limit,
            "sort": sort,
        }

    @app.get("/api/catalog/near")
    async def catalog_near(hz: int, tol: int = 5000, service: AppService = Depends(get_service)):
        entries = service.catalog_near(hz, tolerance_hz=tol)
        return {"entries": service.catalog_entry_dicts(entries)}

    @app.get("/api/catalog/sources")
    async def catalog_sources(service: AppService = Depends(get_service)):
        return service.catalog_sources_summary()

    @app.post("/api/catalog/refresh")
    async def catalog_refresh(body: CatalogRefreshIn, service: AppService = Depends(get_service)):
        return await service.catalog_refresh(body.source)

    @app.post("/api/catalog/import")
    async def catalog_import(body: CatalogImportIn, service: AppService = Depends(get_service)):
        return service.catalog_import_file(body.path, body.format)

    # --- allocations (frequency allocation reference, docs/allocations.md) ---

    @app.get("/api/allocations/lookup")
    async def allocations_lookup(
        hz: int,
        jurisdiction: str | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.allocation_context(hz, jurisdiction)

    @app.get("/api/allocations/bands")
    async def allocations_bands(
        lo_hz: int,
        hi_hz: int,
        jurisdiction: str | None = None,
        column: str | None = None,
        service: AppService = Depends(get_service),
    ):
        return service.allocation_bands(lo_hz, hi_hz, jurisdiction, column=column)

    @app.get("/api/allocations/sources")
    async def allocations_sources(service: AppService = Depends(get_service)):
        return service.allocation_sources()

    @app.post("/api/allocations/refresh")
    async def allocations_refresh(
        body: AllocationRefreshIn, service: AppService = Depends(get_service)
    ):
        return await service.allocation_refresh(body.source)

    @app.post("/api/tune/catalog/{entry_id}")
    async def tune_catalog(entry_id: str, service: AppService = Depends(get_service)):
        report = await service.tune_catalog(entry_id)
        return restore_report_to_dict(report)

    @app.post("/api/catalog/{entry_id}/save")
    async def save_catalog_entry(entry_id: str, service: AppService = Depends(get_service)):
        signal = service.save_catalog_entry(entry_id)
        return signal.model_dump(mode="json")

    # --- LISTEN presets ------------------------------------------------

    @app.get("/api/listen")
    async def listen(preset: str, limit: int = 30, service: AppService = Depends(get_service)):
        return await service.listen(preset, limit=limit)

    # --- profiles -------------------------------------------------------

    @app.get("/api/profiles")
    async def list_profiles(service: AppService = Depends(get_service)):
        return [p.model_dump(mode="json") for p in service.profiles.list()]

    @app.post("/api/profiles")
    async def create_profile(body: ProfileIn, service: AppService = Depends(get_service)):
        profile = ReceiverProfile(**body.model_dump())
        service.profiles.add(profile)
        return profile.model_dump(mode="json")

    @app.get("/api/profiles/{profile_id}")
    async def get_profile(profile_id: str, service: AppService = Depends(get_service)):
        profile = service.profiles.get(profile_id)
        if profile is None:
            raise HTTPException(404, "profile not found")
        return profile.model_dump(mode="json")

    @app.patch("/api/profiles/{profile_id}")
    async def patch_profile(
        profile_id: str, patch: ProfilePatch, service: AppService = Depends(get_service)
    ):
        existing = service.profiles.get(profile_id)
        if existing is None:
            raise HTTPException(404, "profile not found")
        data = patch.model_dump(exclude_unset=True)
        updated = ReceiverProfile.model_validate(existing.model_dump() | data)
        service.profiles.update(updated)
        return updated.model_dump(mode="json")

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str, service: AppService = Depends(get_service)):
        if service.profiles.get(profile_id) is None:
            raise HTTPException(404, "profile not found")
        service.profiles.delete(profile_id)
        return {"deleted": True}

    @app.post("/api/profiles/{profile_id}/apply")
    async def apply_profile(profile_id: str, service: AppService = Depends(get_service)):
        report = await service.apply_profile(profile_id)
        return restore_report_to_dict(report)

    # --- tags -------------------------------------------------------------

    @app.get("/api/tags")
    async def list_tags(service: AppService = Depends(get_service)):
        return [t.model_dump(mode="json") for t in service.tags.list()]

    @app.post("/api/tags")
    async def create_tag(body: TagIn, service: AppService = Depends(get_service)):
        tag = Tag(name=body.name, color=body.color)
        try:
            service.tags.add(tag)
        except sqlite3.IntegrityError as exc:
            # UNIQUE(tags.name) -- often a category tag the app seeded itself.
            raise HTTPException(409, "tag name already in use") from exc
        return tag.model_dump(mode="json")

    @app.patch("/api/tags/{tag_id}")
    async def patch_tag(tag_id: str, patch: TagPatch, service: AppService = Depends(get_service)):
        existing = service.tags.get(tag_id)
        if existing is None:
            raise HTTPException(404, "tag not found")
        data = patch.model_dump(exclude_unset=True)
        updated = Tag.model_validate(existing.model_dump() | data)
        try:
            service.tags.update(updated)
        except sqlite3.IntegrityError as exc:
            # UNIQUE(tags.name): another tag already has the requested name.
            raise HTTPException(409, "tag name already in use") from exc
        return updated.model_dump(mode="json")

    # --- palette ------------------------------------------------------------

    @app.get("/api/palette")
    async def palette(q: str = "", service: AppService = Depends(get_service)):
        tags = {t.id: t for t in service.tags.list()}
        # Candidates come out of SQL rather than a LIMIT-ed page of the whole
        # library, so an exact match can't be cut off before rank() sees it.
        # rank() itself branches on parse_frequency, so mirror that here: a
        # frequency query wants neighbours, anything else wants text matches.
        freq = parse_frequency(q) if q else None
        if freq is not None:
            candidates = service.signals.near(freq, service.settings.nearby_hz)
        elif q:
            candidates = service.signals.search(q, limit=1000)
        else:
            candidates = []
        results = rank(
            q,
            candidates,
            service.profiles.list(),
            tags,
            nearby_hz=service.settings.nearby_hz,
        )
        out = [dataclasses.asdict(r) for r in results]
        if q and service.settings.features.catalog:
            entries, _count = service.catalog_search(q, on_air=True, limit=5)
            for entry in entries:
                if entry.frequency_hz is None:
                    continue  # untunable reference row (DME/TACAN, radar site)
                window = f"{entry.start_utc or '0000'}-{entry.stop_utc or '2359'} UTC"
                out.append(
                    {
                        "kind": "catalog",
                        "id": entry.id,
                        "title": entry.station,
                        "subtitle": f"{format_mhz(entry.frequency_hz)} · {window}",
                        "score": 0.0,
                        "frequency_hz": entry.frequency_hz,
                        "color": None,
                        "tag_ids": [],
                    }
                )
        return out

    # --- tune / history -------------------------------------------------

    @app.post("/api/tune/signal/{signal_id}")
    async def tune_signal(signal_id: str, service: AppService = Depends(get_service)):
        report = await service.tune_signal(signal_id)
        return restore_report_to_dict(report)

    @app.post("/api/tune/frequency")
    async def tune_frequency(body: TuneFrequencyIn, service: AppService = Depends(get_service)):
        report = await service.tune_frequency(
            body.hz, body.demodulator, body.filter_bandwidth_hz, manual_mode=body.manual_mode
        )
        return restore_report_to_dict(report)

    @app.post("/api/receiver/antenna")
    async def set_antenna(body: AntennaIn, service: AppService = Depends(get_service)):
        try:
            report = await service.set_antenna(body.antenna)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return restore_report_to_dict(report)

    @app.post("/api/history/back")
    async def history_back(service: AppService = Depends(get_service)):
        report = await service.back()
        return {"report": restore_report_to_dict(report) if report is not None else None}

    @app.post("/api/history/forward")
    async def history_forward(service: AppService = Depends(get_service)):
        report = await service.forward()
        return {"report": restore_report_to_dict(report) if report is not None else None}

    @app.get("/api/history")
    async def get_history(service: AppService = Depends(get_service)):
        return {
            "position": service.history.position,
            "entries": [s.model_dump(mode="json") for s in service.history.entries],
        }

    # --- journal --------------------------------------------------------

    @app.get("/api/journal")
    async def get_journal(day: str | None = None, service: AppService = Depends(get_service)):
        day = day or datetime.now(UTC).date().isoformat()
        entries = service.journal_repo.list_day(day)
        state_ids = [e.state_id for e in entries if e.state_id is not None]
        states_by_id = {s.id: s for s in service.states.get_many(state_ids)}
        out = []
        for entry in entries:
            state = states_by_id.get(entry.state_id) if entry.state_id else None
            signal = service.signals.get(entry.signal_id) if entry.signal_id else None
            out.append(
                {
                    **entry.model_dump(mode="json"),
                    "state": state.model_dump(mode="json") if state is not None else None,
                    "signal_name": signal.name if signal is not None else None,
                    "signal_color": signal.color if signal is not None else None,
                    "tag_ids": signal.tag_ids if signal is not None else [],
                }
            )
        return out

    @app.get("/api/journal/days")
    async def journal_days(service: AppService = Depends(get_service)):
        return {"days": service.journal_repo.days()}

    # Backups are a verified database snapshot; legacy library JSON remains a merge.
    def backup_media_roots(service):
        return [Path(service.settings.recorder_root).expanduser(),
                service._resolve_calls_dir(service.settings),
                service._resolve_fax_dir(service.settings),
                Path(service.settings.iqscan_scan_root).expanduser()]

    def archive_response(build, filename):
        import tempfile

        from starlette.background import BackgroundTask

        temp = tempfile.TemporaryDirectory(prefix="msdr-export-")
        path = Path(temp.name) / filename
        try:
            build(path)
        except Exception:
            temp.cleanup()
            raise
        return FileResponse(path, filename=filename, media_type="application/zip",
                            background=BackgroundTask(temp.cleanup))

    @app.get("/api/backup")
    async def backup(include_media: bool = False, service: AppService = Depends(get_service)):
        from msdr.storage.backup import create_backup

        # Read the live connection only on its owning event loop. Compression
        # and media I/O use a private snapshot so live IQ processing can continue.
        snapshot = Database(":memory:")
        snapshot.connect()
        service.db.conn.backup(snapshot.conn)
        roots = backup_media_roots(service)
        task = asyncio.create_task(asyncio.to_thread(
            archive_response,
            lambda path: create_backup(snapshot, path, include_media=include_media,
                                       media_roots=roots),
            "msdr-backup.zip",
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The worker still owns the snapshot until its archive is finished.
            response = await task
            await response.background()
            raise
        finally:
            snapshot.close()

    @app.post("/api/backup/restore")
    async def restore_backup_route(request: Request, service: AppService = Depends(get_service)):
        import tempfile
        import zipfile

        from msdr.storage.backup import restore_backup

        def require_idle():
            if (service.scanner.state != "idle" or service._band_scan_active
                    or (service.recorder and service.recorder.status().get("state") != "off")
                    or (service.survey and service.survey.status().get("active"))
                    or (service.decoder and service.decoder.status().get("active"))):
                raise HTTPException(
                    409, "stop scanning, survey, decoding and disarm before restoring"
                )

        require_idle()
        with tempfile.TemporaryDirectory(prefix="msdr-restore-") as temp:
            source = Path(temp) / "backup.zip"
            with source.open("wb") as dest:
                async for chunk in request.stream():
                    dest.write(chunk)
            require_idle()
            try:
                report = restore_backup(
                    service.db, source,
                    media_root=Path(service.settings.recorder_root).expanduser() / "restored",
                )
                service.band_scan.current = None
                return report
            except (ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile) as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.get("/api/signals/{signal_id}/evidence-bundle")
    async def signal_evidence_bundle(signal_id: str, service: AppService = Depends(get_service)):
        from msdr.storage.backup import create_evidence_bundle

        try:
            return archive_response(
                lambda path: create_evidence_bundle(service.db, signal_id, path),
                "msdr-evidence.zip",
            )
        except KeyError as exc:
            raise HTTPException(404, "signal not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    # --- export / import --------------------------------------------------

    @app.get("/api/export")
    async def export(service: AppService = Depends(get_service)):
        return export_all(service.db)

    @app.post("/api/import")
    async def import_(doc: dict, service: AppService = Depends(get_service)):
        try:
            report = import_all(service.db, doc)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return report.model_dump(mode="json")

    # --- settings -----------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings(service: AppService = Depends(get_service)):
        return service.settings.model_dump(mode="json")

    @app.patch("/api/settings")
    async def patch_settings(body: SettingsPatch, service: AppService = Depends(get_service)):
        updated = await service.apply_settings(body.model_dump(exclude_unset=True))
        return updated.model_dump(mode="json")

    @app.get("/api/timezones")
    async def list_timezones(service: AppService = Depends(get_service)):
        return service.available_timezones()

    # --- websocket ------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not _origin_is_local(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        service: AppService = websocket.app.state.service
        await websocket.accept()
        await websocket.send_json({"type": "state", **service.state_payload()})
        q = service.broadcaster.subscribe()
        try:
            while True:
                outgoing = asyncio.ensure_future(next_outgoing(q))
                incoming = asyncio.ensure_future(websocket.receive_text())
                done, pending = await asyncio.wait(
                    {outgoing, incoming}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if incoming in done:
                    # Raises WebSocketDisconnect on close; text is otherwise ignored.
                    incoming.result()
                if outgoing in done:
                    await websocket.send_json(outgoing.result())
                if q.slow:
                    # Broadcaster gave up on this client (queue stuck full) --
                    # close so the browser reconnects with a fresh queue.
                    await websocket.close(code=1013)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            service.broadcaster.unsubscribe(q)

    # --- review audio transport (S3) -----------------------------------------

    @app.websocket("/ws/audio")
    @app.websocket("/ws/live-audio")
    async def ws_audio(websocket: WebSocket):
        if not _origin_is_local(websocket.headers.get("origin")):
            await websocket.close(code=1008)
            return
        service: AppService = websocket.app.state.service
        await websocket.accept()
        # Review keeps its replay-only endpoint; live inspection is explicitly
        # started and has a separate endpoint and single-consumer reservation.
        live = websocket.url.path == "/ws/live-audio"
        rx = service.workbench.live_rx if live else service.virtual_rx
        if rx is None:
            await websocket.send_json(
                {"type": "unavailable", "reason": "no inspection receiver running"}
            )
            await websocket.close()
            return
        if (service.workbench.audio_active if live else service._audio_ws_active):
            await websocket.send_json(
                {"type": "unavailable", "reason": "audio already streaming to another client"}
            )
            await websocket.close()
            return
        try:
            msg = await websocket.receive_json()
        except (WebSocketDisconnect, ValueError):
            return
        if not isinstance(msg, dict) or msg.get("type") != "start":
            await websocket.close(code=1003)
            return

        # The handshake awaited input: recheck before reserving the single
        # consumer, otherwise two simultaneous handshakes can split the FIFO.
        if (service.workbench.audio_active if live else service._audio_ws_active):
            await websocket.send_json(
                {"type": "unavailable", "reason": "audio already streaming to another client"}
            )
            await websocket.close()
            return
        if live:
            service.workbench.audio_active = True
        else:
            service._audio_ws_active = True
        last_generation: int | None = None
        last_channel_generation: int | None = None
        audio_iterator = rx.audio().__aiter__()
        pending_audio = None
        pending_recv = None
        try:
            while (service.workbench.live_rx if live else service.virtual_rx) is rx:
                if pending_audio is None:
                    pending_audio = asyncio.create_task(anext(audio_iterator))
                if pending_recv is None:
                    # Concurrent with the sample wait, otherwise a disconnect
                    # during a sample gap (receiver paused, no audio) goes
                    # unnoticed -- nothing else here ever reads the socket --
                    # and the single-consumer slot stays reserved until a
                    # sample eventually arrives, if ever.
                    pending_recv = asyncio.create_task(websocket.receive_text())
                done, _ = await asyncio.wait(
                    {pending_audio, pending_recv}, timeout=0.5, return_when=asyncio.FIRST_COMPLETED
                )
                if pending_recv in done:
                    # Raises WebSocketDisconnect on close; text is otherwise ignored.
                    pending_recv.result()
                    pending_recv = None
                    continue
                if pending_audio not in done:
                    continue
                try:
                    block = pending_audio.result()
                except StopAsyncIteration:
                    break
                pending_audio = None
                if (service.workbench.live_rx if live else service.virtual_rx) is not rx:
                    break
                # A flush is due on either a source discontinuity (seek/loop/
                # retune -- AudioBlock.generation) or a plain retune on the
                # same source (VirtualReceiver.status()["channel_generation"],
                # which bumps without touching the source's own generation).
                channel_generation = rx.status().get("channel_generation")
                needs_flush = (
                    block.generation != last_generation
                    or channel_generation != last_channel_generation
                )
                if needs_flush:
                    await websocket.send_json({"type": "flush", "generation": block.generation})
                    last_generation = block.generation
                    last_channel_generation = channel_generation
                header = struct.pack(
                    "<IIi", block.generation, block.sample_index & 0xFFFFFFFF, block.rate
                )
                await websocket.send_bytes(header + block.pcm)
        except WebSocketDisconnect:
            pass
        finally:
            for task in (pending_audio, pending_recv):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, StopAsyncIteration, WebSocketDisconnect
                    ):
                        await task
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()
            if live:
                service.workbench.audio_active = False
            else:
                service._audio_ws_active = False

    # --- static UI (Task 9); only mounted if the directory exists ----------

    ui_dir = Path(__file__).resolve().parent.parent / "ui"
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app
