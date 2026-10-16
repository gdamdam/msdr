"""Passive Wi-Fi reception (AWUS036ACH, docs/wifi.md): the API-facing mixin
over `msdr.wifi.supervisor.WifiSupervisor` and `msdr.wifi.sessions`.

AppService mixin: methods moved here mirror the shape of RecorderServiceMixin
(msdr/api/services/recorder.py) -- all attributes they use are initialised in
AppService.__init__. This module (and the wifi package it wraps) never
touches `self.adapter`, `self.confirmed`, receiver ownership, the pre-roll
recorder, the scanner, survey or tracking.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from pathlib import Path

from fastapi import HTTPException

from msdr import oui
from msdr.remoteid import transport as remoteid_transport
from msdr.remoteid.tracker import AddressTrack, UasGroup
from msdr.settings import Settings
from msdr.tasks import spawn
from msdr.wifi.observations import NetworkTable
from msdr.wifi.sensing import SensingAccumulator
from msdr.wifi.supervisor import WifiSupervisor
from msdr.wifi.survey import ChannelSurvey

# Upper bound on a Stop's background half (cancel the hop task, restore the
# prior capture): two helper restarts plus margin.
SURVEY_STOP_TIMEOUT_S = 20.0

# IE id -> display name (docs/wifi.md "API"). IDs not listed here show as
# name=None in the API response; the UI falls back to "unknown".
_IE_NAMES: dict[int, str] = {
    0: "SSID",
    1: "Supported Rates",
    3: "DS Parameter Set",
    5: "TIM",
    7: "Country",
    45: "HT Capabilities",
    48: "RSN",
    50: "Extended Supported Rates",
    61: "HT Operation",
    127: "Extended Capabilities",
    191: "VHT Capabilities",
    192: "VHT Operation",
    221: "Vendor Specific",
    255: "Extension",
}
# IE 255 (Extension) ext_id -> display name.
_IE_EXT_NAMES: dict[int, str] = {
    35: "HE Capabilities",
    36: "HE Operation",
}


def _ie_dict(ie) -> dict:
    """An InformationElement as JSON-safe {id, ext_id, name, length, hex} --
    `ie.data` is attacker/neighbour-controlled bytes (not necessarily UTF-8:
    this is what used to reach FastAPI's encoder directly and raise
    UnicodeDecodeError), so it's hex-encoded here at the API boundary and
    never passed through as raw bytes."""
    name = _IE_NAMES.get(ie.id)
    if ie.id == 255 and ie.ext_id is not None:
        name = _IE_EXT_NAMES.get(ie.ext_id)
    return {
        "id": ie.id,
        "ext_id": ie.ext_id,
        "name": name,
        "length": len(ie.data),
        "hex": ie.data.hex(),
    }


def _row_summary(d: dict) -> dict:
    """NetworkEntry.summary()/details() dict -> the UI contract's row shape
    (docs/wifi.md "API"): renames security_summary -> security, folds the
    two separate signal_raw_code/signal_source fields into one nested
    object (or null when nothing was ever reported)."""
    signal = None
    if d["signal_raw_code"] is not None:
        kind = "dbm" if d["signal_source"] == "dbm" else "raw"
        signal = {
            "kind": kind,
            "latest": d["signal_raw_code"],
            # Median of recent samples: what the table shows and sorts by.
            "median": d["signal_median"],
            "samples": d["signal_samples"],
        }
    return {
        "bssid": d["bssid"],
        "ssid": d["ssid"],
        "ssid_hidden": d["ssid_hidden"],
        "revealed_ssids": d["revealed_ssids"],
        "channel": d["channel"],
        "channel_source": d["channel_source"],
        "channel_mismatch": d["channel_mismatch"],
        "security": d["security_summary"],
        "signal": signal,
        "first_seen": d["first_seen"],
        "last_seen": d["last_seen"],
        "beacons": d["beacons"],
        "probe_responses": d["probe_responses"],
        "duplicates": d["duplicates"],
        # IEEE registry vendor once downloaded (msdr/oui.py); a locally
        # administered BSSID has none by design and says so instead.
        "vendor": oui.vendor(d["bssid"]),
        "local_admin": oui.is_locally_administered(d["bssid"]),
    }


def oui_path(settings: Settings) -> Path | None:
    """Downloaded IEEE vendor names, next to msdr.db; None in memory (tests)."""
    if str(settings.db_path) == ":memory:":
        return None
    return Path(settings.db_path).parent / "wifi" / "oui_vendors.json"


def _details_shape(d: dict) -> dict:
    """Row fields plus the details-only fields (docs/wifi.md "API"): IEs and
    RSN/WPA1 as JSON-safe values, HT/VHT/HE presence renamed to short keys
    the UI chips on directly, signal history as [[ts, value], ...] pairs."""
    out = _row_summary(d)
    out.update(
        ies=[_ie_dict(ie) for ie in d["ies"]],
        rsn=dataclasses.asdict(d["rsn"]) if d["rsn"] is not None else None,
        wpa1=dataclasses.asdict(d["wpa1"]) if d["wpa1"] is not None else None,
        ht=d["ht_present"],
        vht=d["vht_present"],
        he=d["he_present"],
        country=d["country"],
        beacon_interval=d["beacon_interval"],
        capabilities={
            "ESS": d["cap_ess"],
            "IBSS": d["cap_ibss"],
            "Privacy": d["cap_privacy"],
        },
        ssid_history=d["ssid_history"],
        signal_history=[[s["ts"], s["raw_code"]] for s in d["signal_history"]],
        recent=[
            {
                "n": r["n"],
                "ts": r["ts"],
                "subtype": r["subtype"],
                "length": r["length"],
                "bad_fcs": r["bad_fcs"],
            }
            for r in d["recent"]
        ],
    )
    return out


def _session_row(m: dict) -> dict:
    """WifiRecordingSession.meta()/import_pcap() dict -> the sessions-list
    contract row (docs/wifi.md "API"): started/stopped -> started_at/
    stopped_at, health_notes (a list, possibly empty) folded into one
    "health" string."""
    notes = m.get("health_notes") or []
    return {
        "id": m["id"],
        "started_at": m.get("started"),
        "stopped_at": m.get("stopped"),
        "channel": m.get("channel"),
        "frames": m.get("frames", 0),
        "bytes": m.get("bytes", 0),
        "drops": m.get("drops", 0),
        "imported": m.get("imported", False),
        "health": "; ".join(notes) if notes else "ok",
    }


def _position_dict(fix) -> dict | None:
    if fix is None:
        return None
    return {
        "latitude_deg": fix.latitude_deg,
        "longitude_deg": fix.longitude_deg,
        "altitude_m": fix.altitude_m,
        "received_at": fix.received_at,
    }


def _drone_position_dict(fix) -> dict | None:
    if fix is None:
        return None
    return {
        "status": fix.status,
        "height_type": fix.height_type,
        "direction_deg": fix.direction_deg,
        "speed_horizontal_ms": fix.speed_horizontal_ms,
        "speed_vertical_ms": fix.speed_vertical_ms,
        "latitude_deg": fix.latitude_deg,
        "longitude_deg": fix.longitude_deg,
        "altitude_baro_m": fix.altitude_baro_m,
        "altitude_geo_m": fix.altitude_geo_m,
        "height_m": fix.height_m,
        "reported_timestamp_s": fix.reported_timestamp_s,
        "received_at": fix.received_at,
    }


def _address_track_dict(track: AddressTrack) -> dict:
    return {
        "address": track.address,
        "sources": sorted(track.sources),
        "uas_id_hex": track.uas_id.hex() if track.uas_id is not None else None,
        "uas_id_text": track.uas_id_text,
        "ua_type": track.ua_type,
        "drone_position": _drone_position_dict(track.drone_position),
        "operator_position": _position_dict(track.operator_position),
        "takeoff_position": _position_dict(track.takeoff_position),
        "self_id": track.self_id,
        "operator_id": track.operator_id,
        "last_seen": track.last_seen,
    }


def _uas_group_dict(group: UasGroup) -> dict:
    return {
        "key": group.key,
        "uas_id_hex": group.uas_id.hex() if group.uas_id is not None else None,
        "conflict": group.conflict,
        "tracks": [_address_track_dict(t) for t in group.tracks],
    }


class WifiServiceMixin:
    def _build_wifi(self, settings: Settings) -> WifiSupervisor:
        path = oui_path(settings)
        if path is not None:
            oui.load(path)
        else:
            oui.set_names(None)
        sup = WifiSupervisor(
            helper_path=settings.wifi.helper_path,
            helper_command=settings.wifi.helper_command,
            on_status=self._on_wifi_status,
        )
        sup.sensing = self._wifi_sensing_for(settings)
        return sup

    def _wifi_sensing_for(self, settings: Settings) -> SensingAccumulator | None:
        """The sensing accumulator for wifi.sensing_source, kept across supervisor
        rebuilds while the source is unchanged; None when sensing is off."""
        src = settings.wifi.sensing_source
        acc = getattr(self, "_wifi_sensing_acc", None)
        if not src:
            acc = None
        elif acc is None or acc.source != src:
            acc = SensingAccumulator(src)
        self._wifi_sensing_acc = acc
        return acc

    def wifi_sensing(self) -> dict:
        """GET /api/wifi/sensing: latest row, presence, crossing and breathing for the
        configured source, or `enabled: false` when no source is set."""
        acc = self.wifi.sensing
        if acc is None:
            return {"enabled": False}
        return {"enabled": True, **acc.snapshot(time.time())}

    # --- IEEE vendor names (msdr/oui.py) ------------------------------------

    def wifi_vendors_status(self) -> dict:
        names = oui.names()
        return {"downloaded": names is not None, **(names.summary() if names else {}),
                "source": "https://standards-oui.ieee.org/"}

    def wifi_network_summary(self, window_s: float = 60.0) -> dict:
        table = self.wifi.table
        if table is None:
            return {"window_s": window_s, "total": 0, "access_points": 0, "hidden": 0,
                    "local_admin": 0, "bands": [], "security": [], "makers": []}
        return table.network_summary(time.time(), window_s=window_s)

    async def wifi_vendors_refresh(self) -> dict:
        path = oui_path(self.settings)
        if path is None:
            raise HTTPException(409, "no data folder (in-memory database)")
        try:
            await asyncio.to_thread(oui.refresh, path)
        except Exception as exc:  # network, HTTP or CSV -- keep the old file
            raise HTTPException(502, f"download failed: {exc}") from exc
        return self.wifi_vendors_status()

    def _wifi_sessions_root(self) -> Path:
        # _recordings_root() comes from RecorderServiceMixin -- same
        # recordings root as SigMF IQ recordings, own "wifi" subfolder
        # (docs/wifi.md: "wifi/<session-id>/part-NNN.pcap").
        return self._recordings_root() / "wifi"

    def _require_wifi_action_enabled(self) -> None:
        # Only the routes that actually operate the receiver/recorder refuse
        # while the feature is off (docs/wifi.md): start, session start/stop,
        # import. Every read (status, devices, networks, sessions list/
        # export) works regardless -- the Setup card needs status/devices to
        # work while disabled to show "stopped"/enabled: false in the first
        # place.
        if not self.settings.wifi.enabled:
            raise HTTPException(409, "Enable Wi-Fi in Setup first")

    # --- status / websocket -------------------------------------------------

    def _on_wifi_status(self, status: dict) -> None:
        """Called synchronously from WifiSupervisor (reader/tick tasks) --
        never awaited there, so the actual publish is scheduled instead."""
        if self._wifi_session is not None:
            self._wifi_session.note_drops(status.get("drops", 0))
            err = status.get("last_error")
            if err:
                self._wifi_session.add_health_note(err)
        spawn(self._publish_wifi_status())

    async def _publish_wifi_status(self) -> None:
        now = time.time()
        changed = self._wifi_changed_summaries(self._wifi_last_push)
        self._wifi_last_push = now
        await self.broadcaster.publish(
            {"type": "wifi", "status": self.wifi_status(), "changed": changed}
        )

    def _wifi_changed_summaries(self, since: float) -> list[dict]:
        table = self.wifi.table
        if table is None:
            return []
        out = []
        for bssid in table.snapshot_changed_since(since):
            d = table.details(bssid)
            if d is None:
                continue
            out.append(_row_summary(d))
        return out

    def wifi_status(self) -> dict:
        base = self.wifi.status()
        recording = None
        if self._wifi_session is not None:
            recording = {
                "session_id": self._wifi_session.id,
                "started_at": self._wifi_session.started,
                "bytes": self._wifi_session.total_bytes,
                "frames": self._wifi_session.frames,
            }
        return {
            "state": base["state"],
            "enabled": self.settings.wifi.enabled,
            "wanted": base["wanted"],
            "channel": base["channel"],
            # The channel Start would tune to, so the UI can name it while
            # stopped (channel above is None until the helper is running).
            "configured_channel": self.settings.wifi.channel,
            "frequency_mhz": base["frequency_mhz"],
            "frames": base["frames"],
            "frame_rate": base["frame_rate"],
            "drops": base["drops"],
            "bad_fcs": base["bad_fcs"],
            "parse_errors": base["parse_errors"],
            "last_frame_at": base["last_frame_at"],
            "last_error": base["last_error"],
            "last_exit_code": base["last_exit_code"],
            "retry_attempt": base["retry_attempt"],
            "retry_at": base["retry_at"],
            "helper": base["helper"],
            "device": self._wifi_last_device,
            "recording": recording,
            "network_count": base["network_count"],
        }

    async def wifi_devices(self) -> dict:
        result = await self.wifi.devices()
        rows = result.get("devices") or []
        if rows:
            d = rows[0]
            self._wifi_last_device = {
                "product": d.get("product"),
                "serial": d.get("serial"),
                "bus": d.get("bus"),
                "address": d.get("address"),
            }
        return result

    async def _apply_pending_wifi_settings(self) -> None:
        # Cleared only after the stop/rebuild awaits; without the lock a second
        # caller would start the old supervisor and then lose it to the rebuild.
        async with self._wifi_settings_lock:
            await self._apply_pending_wifi_settings_locked()

    async def _apply_pending_wifi_settings_locked(self) -> None:
        if not getattr(self, "_wifi_settings_pending", False):
            return
        self._wifi_finalize_session("Wi-Fi helper settings changed")
        await self.wifi.stop()
        self.wifi = self._build_wifi(self.settings)
        self._wifi_settings_pending = False
        self._sync_history_sinks()

    async def wifi_start(self, channel: int) -> dict:
        self._require_wifi_action_enabled()
        await self._wifi_survey_stop_if_running()
        await self._apply_pending_wifi_settings()
        await self.wifi.start(channel, configured_channel=channel)
        return self.wifi_status()

    async def wifi_stop(self) -> dict:
        await self._wifi_survey_stop_if_running()
        self._wifi_finalize_session("wifi stopped")
        await self.wifi.stop()
        return self.wifi_status()

    # --- channel survey (docs/history.md "Wi-Fi channel survey") ------------

    async def _wifi_survey_stop_if_running(self) -> None:
        """A plain start/stop must stop a running survey first (contract) --
        not gated on the feature being enabled, same as plain wifi_stop.
        A stop already in progress is waited for, not repeated."""
        survey = self._wifi_survey
        if survey is not None and survey.state == "running":
            await survey.stop()
            await self._wifi_survey_await_watch()
        await self._wifi_survey_await_stop()

    async def _wifi_survey_await_stop(self) -> None:
        task = self._wifi_survey_stop_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _wifi_survey_finish_stop(self, survey: ChannelSurvey) -> None:
        """Background half of a Stop: cancel the hop task and restore the
        prior capture (each a helper restart, a few seconds). Bounded, so a
        hung helper can never leave the survey stuck in "stopping"."""
        try:
            await asyncio.wait_for(
                self._wifi_survey_stop_and_restore(survey), SURVEY_STOP_TIMEOUT_S
            )
        except TimeoutError:
            survey.error = (
                f"stop took over {SURVEY_STOP_TIMEOUT_S:.0f} s; Wi-Fi capture may need a restart"
            )
        finally:
            if survey.state == "stopping":
                survey.state = "stopped"
                survey.ended_at = time.time()
            self._wifi_survey_stop_task = None

    async def _wifi_survey_stop_and_restore(self, survey: ChannelSurvey) -> None:
        # Stays "stopping" until the prior capture is back (finally above).
        await survey.stop(finalize=False)
        await self._wifi_survey_await_watch()

    async def _wifi_survey_await_watch(self) -> None:
        task, self._wifi_survey_watch_task = self._wifi_survey_watch_task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _wifi_survey_watch(self, survey: ChannelSurvey) -> None:
        """Waits for the survey's own hop task to finish -- on a stop() it's
        already done by the time this runs; on natural completion
        (done/failed) this is what notices and restores whatever capture was
        running before the survey started."""
        task = survey.task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._wifi_survey_restore()

    async def _wifi_survey_restore(self) -> None:
        prior, self._wifi_survey_prior = self._wifi_survey_prior, None
        if prior is None:
            return
        if prior["running"] and prior["channel"] is not None:
            await self.wifi.start(prior["channel"], configured_channel=prior["channel"])
        else:
            await self.wifi.stop()

    async def wifi_survey_start(self, channels: list[int], dwell_s: float, rounds: int) -> dict:
        self._require_wifi_action_enabled()
        if self._wifi_session is not None:
            raise HTTPException(409, "a wifi session is recording")
        if self._wifi_survey is not None and self._wifi_survey.state in ("running", "stopping"):
            raise HTTPException(409, "a channel survey is already running")
        self._wifi_survey_prior = {"running": self.wifi.running, "channel": self.wifi.channel}
        await self._apply_pending_wifi_settings()
        survey = ChannelSurvey(self.wifi, channels=channels, dwell_s=dwell_s, rounds=rounds)
        self._wifi_survey = survey
        survey.start()
        self._wifi_survey_watch_task = asyncio.create_task(self._wifi_survey_watch(survey))
        return self.wifi_survey_status()

    async def _wifi_survey_shutdown(self) -> None:
        """Called from AppService.stop(): cancel any running survey without
        going through the normal restore-prior-capture path -- the whole app
        is stopping, so there's nothing left to restore to."""
        for attr in ("_wifi_survey_stop_task", "_wifi_survey_watch_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        survey, self._wifi_survey = self._wifi_survey, None
        if survey is not None and survey.state in ("running", "stopping"):
            await survey.stop()
        self._wifi_survey_prior = None

    async def wifi_survey_stop(self) -> dict:
        survey = self._wifi_survey
        if survey is None or survey.state != "running":
            raise HTTPException(409, "no channel survey running")
        # Reply at once: the hop task's cancel and the prior-capture restore
        # are helper restarts, finished in the background (state "stopping").
        survey.request_stop()
        self._wifi_survey_stop_task = asyncio.create_task(self._wifi_survey_finish_stop(survey))
        return self.wifi_survey_status()

    def wifi_survey_status(self) -> dict:
        if self._wifi_survey is None:
            return {
                "state": "idle",
                "channels": [],
                "dwell_s": None,
                "rounds": None,
                "round": 0,
                "current_channel": None,
                "channel_ends_at": None,
                "started_at": None,
                "ended_at": None,
                "error": None,
                "results": [],
            }
        return self._wifi_survey.status()

    # --- networks (live or an imported/replayed session) --------------------

    def _table_for_source(self, source: str) -> NetworkTable | None:
        if source == "live":
            return self.wifi.table
        if source.startswith("session:"):
            session_id = source[len("session:") :]
            meta = self.wifi_sessions.get(session_id)
            if meta is None:
                raise HTTPException(404, f"wifi session not found: {session_id!r}")
            return self.wifi_sessions.replay_table(
                session_id, configured_channel=meta.get("channel") or self.settings.wifi.channel
            )
        raise HTTPException(400, f"unknown source: {source!r}")

    def wifi_clear_networks(self) -> dict:
        """Empty the live network table; capture and any recording continue."""
        if self.wifi.table is not None:
            self.wifi.table.clear()
        return {"ok": True}

    def wifi_live(self, *, after: int = 0, kind: str = "all", limit: int = 200) -> dict:
        """Frames received since sequence `after`, newest last (live table only)."""
        table = self.wifi.table
        if table is None:
            return {"items": [], "seq": 0, "maxlen": 0}
        return table.live_since(after=after, kind=kind, limit=min(limit, 500))

    def wifi_networks(
        self,
        *,
        source: str = "live",
        q: str | None = None,
        channel: int | None = None,
        offset: int = 0,
        limit: int = 100,
        sort: str = "first_seen",
        order: str = "asc",
    ) -> dict:
        table = self._table_for_source(source)
        if table is None:
            return {"items": [], "total": 0, "configured_channel": None, "since": None}
        rows, total = table.rows(
            q=q, channel=channel, offset=offset, limit=min(limit, 500), sort=sort, order=order
        )
        return {
            "items": [_row_summary(r) for r in rows],
            "total": total,
            "configured_channel": table.configured_channel,
            "since": table.since(),
        }

    def wifi_network_details(self, bssid: str, *, source: str = "live") -> dict:
        table = self._table_for_source(source)
        details = table.details(bssid) if table is not None else None
        if details is None:
            raise HTTPException(404, f"network not found: {bssid!r}")
        return _details_shape(details)

    def wifi_network_frame(self, bssid: str, n: int, *, source: str = "live") -> str:
        table = self._table_for_source(source)
        frame = table.frame(bssid, n) if table is not None else None
        if frame is None:
            raise HTTPException(404, "frame not found")
        return frame.hex()

    # --- sessions (explicit pcap recording) ----------------------------------

    def _wifi_receiver_id(self, devices: dict) -> str:
        rows = devices.get("devices") or []
        if rows:
            d = rows[0]
            ident = d.get("serial") or f"{d.get('bus')}-{d.get('address')}"
            return f"wifi:{ident}"
        return "wifi:unknown"

    def _wifi_session_limit_reached(self) -> None:
        # Called synchronously from WifiSupervisor's aggregator task once the
        # session's write_record() reports its size/duration limit was hit.
        self._wifi_finalize_session("record_max reached")

    def _wifi_finalize_session(self, note: str) -> dict | None:
        session, self._wifi_session = self._wifi_session, None
        if session is None:
            return None
        self.wifi.attach_recording(None)
        session.note_drops(self.wifi.drops)
        session.add_health_note(note)
        return session.finalize()

    async def wifi_session_start(self) -> dict:
        self._require_wifi_action_enabled()
        if self._wifi_session is not None:
            raise HTTPException(409, "a wifi session is already recording")
        if self.wifi.channel is None:
            raise HTTPException(409, "start receiving before recording a session")
        devices = await self.wifi.devices()
        session = self.wifi_sessions.new_session(
            channel=self.wifi.channel,
            receiver_id=self._wifi_receiver_id(devices),
            helper_commit=self.wifi.ready.upstream_commit if self.wifi.ready else None,
            max_bytes=self.settings.wifi.record_max_mb * 1024 * 1024,
            max_seconds=self.settings.wifi.record_max_minutes * 60,
        )
        self._wifi_session = session
        self.wifi.attach_recording(session.write_record, on_full=self._wifi_session_limit_reached)
        return session.meta()

    async def wifi_session_stop(self) -> dict:
        self._require_wifi_action_enabled()
        if self._wifi_session is None:
            raise HTTPException(409, "no wifi session recording")
        meta = self._wifi_finalize_session("stopped")
        assert meta is not None
        return meta

    def wifi_sessions_list(self) -> list[dict]:
        return [_session_row(m) for m in self.wifi_sessions.list_sessions()]

    def wifi_session_export_path(self, session_id: str) -> Path:
        try:
            return self.wifi_sessions.export(session_id)
        except KeyError:
            raise HTTPException(404, f"wifi session not found: {session_id!r}") from None

    def wifi_session_remoteid(self, session_id: str) -> dict:
        """Offline Remote ID decode of a recorded/imported session
        (docs/remote-id.md): no persistence, computed fresh each call so
        decoding the same capture is reproducible."""
        try:
            t = remoteid_transport.decode_wifi_session(self.wifi_sessions, session_id)
        except KeyError:
            raise HTTPException(404, f"wifi session not found: {session_id!r}") from None
        return {"groups": [_uas_group_dict(g) for g in t.groups()]}

    async def wifi_import(self, path: Path) -> dict:
        self._require_wifi_action_enabled()
        return await asyncio.to_thread(
            self.wifi_sessions.import_pcap, path, configured_channel=self.settings.wifi.channel
        )
