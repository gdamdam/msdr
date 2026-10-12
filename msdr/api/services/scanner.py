"""Scanner (v0.12.0) and the scanner finds log.

AppService mixin: methods moved verbatim from msdr/api/service.py;
all attributes they use are initialised in AppService.__init__.
"""

import logging

from fastapi import HTTPException

from msdr.api.schemas import ScanListSourceIn, ScanRangeSourceIn, ScanSpectrumSourceIn, ScanStartIn
from msdr.api.services.allocations import format_services
from msdr.engine.restore import RestoreReport
from msdr.engine.scanner import (
    ListSource,
    ListSourceNotFound,
    RangeSource,
    ScanConfig,
    ScanSourceError,
    Source,
    SpectrumSource,
    default_dwell_ms,
)
from msdr.library.models import Signal

logger = logging.getLogger(__name__)


class ScannerServiceMixin:
    # --- scanner finds log --------------------------------------------
    # Engine hooks passed into ScannerEngine above.

    _CLASSIFICATION_SURVEY_KEYS = (
        "activity",
        "bw_class",
        "modulation",
        "symbol_rate_hz",
        "family",
        "voice",
        "confirmed",
    )

    def _survey_label_near(self, frequency_hz: int) -> dict | None:
        """The active survey's label nearest `frequency_hz`, within +-1
        spectrum bin (matches msdr.survey.Survey._label_by_frequency's own
        tolerance) -- or None when the survey is off/inactive or nothing is
        tracked there."""
        if self.survey is None:
            return None
        status = self.survey.status()
        if not status.get("active"):
            return None
        sample_rate_hz = (status.get("span") or {}).get("sample_rate_hz")
        if not sample_rate_hz:
            return None
        tolerance_hz = sample_rate_hz / 512
        best = None
        best_dist = None
        for label in status.get("labels", []):
            dist = abs(label["frequency_hz"] - frequency_hz)
            if dist <= tolerance_hz and (best is None or dist < best_dist):
                best, best_dist = label, dist
        return best

    async def _classify_find(self, frequency_hz: int) -> dict | None:
        """ScannerEngine's `classify` hook: a survey label snapshot when one's
        tracked at `frequency_hz`, else identify()-only -- bandplan/catalog
        are added from _identify_for_survey either way."""
        bandwidth_hz = int(self.confirmed.props.get("filter_bandwidth") or 12_500)
        survey_label = self._survey_label_near(frequency_hz)
        if survey_label is not None:
            classification = {
                "source": "survey",
                **{key: survey_label.get(key) for key in self._CLASSIFICATION_SURVEY_KEYS},
            }
        else:
            classification = {
                "source": "identify",
                **dict.fromkeys(self._CLASSIFICATION_SURVEY_KEYS),
            }
        candidates = self._identify_for_survey(frequency_hz, bandwidth_hz)
        classification["bandplan"] = next(
            (
                {k: c.get(k) for k in ("name", "category", "confidence", "reason")}
                for c in candidates
                if c.get("provider") == "bandplan"
            ),
            None,
        )
        classification["catalog"] = [
            {k: c.get(k) for k in ("name", "confidence", "reason")}
            for c in candidates
            if c.get("provider") == "catalog"
        ][:3]
        # Every service of the band (docs/allocations.md), frequency-only.
        classification["allocation"] = self.allocation_summary_at(frequency_hz)
        return classification

    def _refresh_ignored_finds(self) -> None:
        self._ignored_find_freqs = set(self.finds.ignored_frequencies())

    def _is_find_ignored(self, frequency_hz: int) -> bool:
        """ScannerEngine's `is_ignored` hook: True within +-6 kHz of any find
        currently marked `ignored` (cached -- called synchronously per
        channel, see _refresh_ignored_finds)."""
        return any(abs(f - frequency_hz) <= 6000 for f in self._ignored_find_freqs)

    async def _on_find(self, find: dict) -> None:
        row, created = self.finds.add_or_merge(find)
        try:
            self.obs_recorder.on_scanner_hit(find, row["id"])
        except Exception:
            logger.exception("observations: on_scanner_hit failed")
        await self.broadcaster.publish({"type": "find", "find": row, "created": created})

    def _require_finds_feature(self) -> None:
        if not self.settings.features.finds:
            raise HTTPException(404, "feature disabled: finds")

    def finds_list(
        self, *, status: str | None = None, limit: int = 200, q: str | None = None
    ) -> dict:
        self._require_finds_feature()
        return {
            "finds": self.finds.list(status=status, limit=limit, q=q),
            "counts": self.finds.counts(),
        }

    def finds_patch(self, find_id: str, *, status: str | None, note: str | None) -> dict:
        self._require_finds_feature()
        existing = self.finds.get(find_id)
        if existing is None:
            raise HTTPException(404, "find not found")
        updated = self.finds.set_status(find_id, status or existing["status"], note=note)
        self._refresh_ignored_finds()
        return updated

    def finds_delete(self, find_id: str) -> dict:
        self._require_finds_feature()
        if not self.finds.delete(find_id):
            raise HTTPException(404, "find not found")
        self._refresh_ignored_finds()
        return {"deleted": True}

    def _classification_notes(self, classification: dict) -> str:
        """One line per known (non-None/non-empty) classification field --
        mirrors msdr.survey.Survey._identity_notes's shape for a find's flat
        classification dict."""
        lines = []
        activity = classification.get("activity")
        if activity:
            lines.append(f"activity: {activity['kind']} (duty {activity['duty']:.2f})")
        if classification.get("bw_class"):
            lines.append(f"bandwidth: {classification['bw_class']}")
        bandplan = classification.get("bandplan")
        if bandplan:
            lines.append(f"bandplan: {bandplan.get('name')}")
        allocation = format_services((classification.get("allocation") or {}).get("services") or [])
        if allocation:
            lines.append(f"allocation: {allocation}")
        catalog = classification.get("catalog") or []
        if catalog:
            lines.append(f"catalog: {catalog[0].get('name')}")
        modulation = classification.get("modulation")
        if modulation:
            lines.append(f"modulation: {modulation.get('kind')}")
        if classification.get("symbol_rate_hz"):
            lines.append(f"symbol rate: {classification['symbol_rate_hz']:.0f} Hz")
        voice = classification.get("voice")
        if voice:
            lines.append(f"voice: {voice.get('kind')}")
        return "\n".join(lines)

    def finds_promote(self, find_id: str, *, name: str | None = None) -> dict:
        self._require_finds_feature()
        find = self.finds.get(find_id)
        if find is None:
            raise HTTPException(404, "find not found")
        classification = find.get("classification") or {}
        confirmed = classification.get("confirmed")
        family = classification.get("family")
        if confirmed and confirmed.get("decoder"):
            decoders = [confirmed["decoder"]]
        elif family and family.get("suggested_decoders"):
            decoders = list(family["suggested_decoders"])
        else:
            decoders = []
        signal = Signal(
            name=name or find.get("label") or f"{find['frequency_hz'] / 1e6:.4f} find",
            frequency_hz=find["frequency_hz"],
            demodulator=find.get("demodulator") or "NFM",
            filter_bandwidth_hz=find.get("bandwidth_hz") or 12_500,
            decoders=decoders,
            notes=self._classification_notes(classification),
        )
        self.create_signal(signal)
        self.finds.set_status(find_id, "saved", signal_id=signal.id)
        self._refresh_ignored_finds()
        return signal.model_dump(mode="json")

    async def finds_tune(self, find_id: str) -> RestoreReport:
        self._require_finds_feature()
        find = self.finds.get(find_id)
        if find is None:
            raise HTTPException(404, "find not found")
        return await self.tune_frequency(
            find["frequency_hz"],
            demodulator=find.get("demodulator"),
            filter_bandwidth_hz=find.get("bandwidth_hz"),
        )

    async def _publish_scan_job(self, job: dict) -> None:
        await self.broadcaster.publish({"type": "scan_job", **job})

    async def _publish_scan_status(self, status: dict) -> None:
        await self.broadcaster.publish({"type": "scan", **status})
        await self._publish_owner()

    async def _publish_scan_hit(self, hit: dict) -> None:
        await self.broadcaster.publish({"type": "scan_hit", "hit": hit})

    # --- scanner (v0.12.0) --------------------------------------------------

    def _scan_source(
        self, s: ScanListSourceIn | ScanRangeSourceIn | ScanSpectrumSourceIn
    ) -> Source:
        if isinstance(s, ScanListSourceIn):
            return ListSource(list_id=s.list_id)
        if isinstance(s, ScanSpectrumSourceIn):
            return SpectrumSource(
                start_hz=s.start_hz,
                stop_hz=s.stop_hz,
                step_hz=s.step_hz,
                level_threshold=s.level_threshold,
                demodulator=s.demodulator,
                filter_bandwidth_hz=s.filter_bandwidth_hz,
            )
        return RangeSource(
            start_hz=s.start_hz,
            stop_hz=s.stop_hz,
            step_hz=s.step_hz,
            demodulator=s.demodulator,
            filter_bandwidth_hz=s.filter_bandwidth_hz,
        )

    def _require_scanner_feature(self) -> None:
        if not self.settings.features.scanner:
            raise HTTPException(404, "feature disabled: scanner")

    async def scan_start(self, body: ScanStartIn) -> dict:
        self._require_scanner_feature()
        self._require_band_scan_idle()
        if self._band_scan_active:
            raise HTTPException(409, "survey band scan owns the receiver")
        if self.scanner.state != "idle":
            raise HTTPException(409, "scan already running")
        # Same check as capture(): a scan can't drive a receiver that isn't
        # fully seeded or connected.
        if not self.confirmed.is_complete() or not self.adapter.connected:
            raise ConnectionError("not connected")
        source = self._scan_source(body.source)
        config = ScanConfig(
            source=source,
            dwell_ms=body.dwell_ms if body.dwell_ms is not None else default_dwell_ms(source),
            settle_ms=body.settle_ms if body.settle_ms is not None else 150,
            threshold_snr_db=(body.threshold_snr_db if body.threshold_snr_db is not None else 10.0),
            hang_s=body.hang_s if body.hang_s is not None else 2.0,
            stop_on_hit=body.stop_on_hit,
            capture_hits=body.capture_hits,
            log_finds=body.log_finds,
            keep_scanning=body.keep_scanning,
            finds_dwell_s=body.finds_dwell_s,
            skip_ignored=body.skip_ignored,
        )
        try:
            return await self.scanner.start(config)
        except ListSourceNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except ScanSourceError as exc:  # too many channels, empty source, bad range, ...
            raise HTTPException(422, str(exc)) from exc

    async def scan_pause(self) -> dict:
        self._require_scanner_feature()
        if self.scanner.state == "idle":
            raise HTTPException(409, "no scan running")
        return await self.scanner.pause("manual pause")

    async def scan_resume(self) -> dict:
        self._require_scanner_feature()
        if self.scanner.state != "paused":
            raise HTTPException(409, "scan not paused")
        return await self.scanner.resume()

    async def scan_stop(self) -> dict:
        self._require_scanner_feature()
        if self.scanner.state == "idle":
            raise HTTPException(409, "no scan running")
        return await self.scanner.stop()

    def scan_skip(self, persist: bool) -> dict:
        self._require_scanner_feature()
        if self.scanner.state == "idle":
            raise HTTPException(409, "no scan running")
        return self.scanner.skip(persist=persist)

    def scan_hold(self) -> dict:
        self._require_scanner_feature()
        if self.scanner.state == "idle":
            raise HTTPException(409, "no scan running")
        return self.scanner.toggle_hold()

    def scan_status(self) -> dict:
        self._require_scanner_feature()
        return self.scanner.status(all_hits=True)
