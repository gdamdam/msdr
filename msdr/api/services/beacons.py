"""NCDXF/IARU beacon monitor (S10).

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import logging

from msdr.settings import Settings
from msdr.tasks import spawn

logger = logging.getLogger(__name__)


try:
    # A fifth package (msdr/survey/beacons.py, S10), built alongside this
    # feature -- same guard as msdr.survey above (see AppService._build_beacons/
    # beacons_*). Imported directly from the submodule (not msdr.survey's own
    # __init__) since that package is owned by a concurrent stream too.
    from msdr.survey.beacons import BeaconError, BeaconMonitor
except ImportError:  # pragma: no cover - msdr.survey.beacons ships with this feature
    BeaconMonitor = None

    class BeaconError(Exception):
        """Placeholder so app.py's exception_handler(BeaconError) always has
        a real type to register, even when msdr.survey.beacons isn't importable."""


class BeaconServiceMixin:
    def _build_beacons(self, settings: Settings):
        if BeaconMonitor is None:  # msdr.survey.beacons not importable -- see guard above
            return None
        return BeaconMonitor(
            adapter=self.adapter,
            tune=self.tune_frequency,
            decode_start=self._beacon_decode_start,
            decode_stop=self._beacon_decode_stop,
            repo=self.beacon_hits,
            publish=self._publish_beacons_message,
            settings_getter=lambda: self.settings.beacons,
            feature_enabled=lambda: self.settings.features.beacons,
        )

    async def _beacon_decode_start(self, frequency_hz: int) -> None:
        """BeaconMonitor's `decode_start` hook: single-channel CW decode on
        the beacon frequency it's currently dwelling on, so its
        note_decode_event can confirm a call sign (mirrors
        _survey_decode_start's use of the decoder)."""
        if self.decoder is None:
            raise BeaconError("decoder unavailable")
        await self.decoder.start_decoding(["cw"], frequency_hz, "manual")

    async def _beacon_decode_stop(self) -> None:
        if self.decoder is None:
            raise BeaconError("decoder unavailable")
        await self.decoder.stop_decoding("beacons")

    def _publish_beacons_message(self, msg: dict) -> None:
        spawn(self.broadcaster.publish(msg))
        if msg.get("type") == "beacons":
            spawn(self._publish_owner())

    async def _start_beacons_runtime(self) -> None:
        # The running flag is set only after the await below; two concurrent
        # toggles (double-click, settings PATCH racing startup) would both
        # pass the check and double-start, so start/stop share a lock.
        async with self._beacons_runtime_lock:
            await self._start_beacons_runtime_locked()

    async def _start_beacons_runtime_locked(self) -> None:
        """Mirrors _start_survey_runtime above -- brings up BeaconMonitor's
        own idle runtime (nothing periodic while idle, per its contract)."""
        if self.beacons is None or self._beacons_running:
            return
        await self.beacons.start()
        self._beacons_running = True

    async def _stop_beacons_runtime(self) -> None:
        async with self._beacons_runtime_lock:
            await self._stop_beacons_runtime_locked()

    async def _stop_beacons_runtime_locked(self) -> None:
        if not self._beacons_running:
            return
        try:
            await self.beacons.stop()
        except Exception:
            logger.exception("beacons: stop failed")
        self._beacons_running = False

    # --- beacon monitor (S10) ------------------------------------------------

    def _require_beacons(self) -> None:
        # Same shape as _require_survey: an unavailable beacons package is a
        # BeaconError -> 409 (app.py handler), not a 404 -- "start refused",
        # not "not found".
        if self.beacons is None:
            raise BeaconError("beacons unavailable")

    def beacons_status(self) -> dict | None:
        return self.beacons.status() if self.beacons is not None else None

    def _check_beacons_refusal(self) -> None:
        """A beacon session retunes the receiver every 3 minutes, so it's
        refused while another feature is actively using the VFO: a survey
        IQ analyze, BAND DECODE, or an in-progress recording."""
        if self.survey is not None and self.survey.status()["analyzer"]["active"]:
            raise BeaconError("survey analyzer active")
        if self.decoder is not None and self.decoder.status()["band"]["active"]:
            raise BeaconError("band decode active")
        if self.recorder is not None and self.recorder.status()["state"] in (
            "recording",
            "finalizing",
        ):
            raise BeaconError("recorder recording")

    async def beacons_start(self, mode: str, frequencies_hz: list[int] | None = None) -> dict:
        self._require_band_scan_idle()
        self._require_beacons()
        self._check_beacons_refusal()
        return await self.beacons.start_session(mode, frequencies_hz)

    async def beacons_stop(self) -> dict:
        self._require_beacons()
        return await self.beacons.stop_session("user")

    def beacon_hits_list(
        self,
        *,
        limit: int = 200,
        since: float | None = None,
        beacon: str | None = None,
    ) -> list[dict]:
        return self.beacon_hits.list(limit=limit, since=since, beacon=beacon)

    def beacon_hits_clear(self) -> int:
        return self.beacon_hits.clear()
