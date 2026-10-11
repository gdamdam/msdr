"""Durable wide-band survey sweep coordinator.

The coordinator owns no radio adapter. Its tune callback must return only
after the requested span is confirmed by the receiver state; this keeps
failed or stale tunes from being counted as covered spectrum.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from msdr.ids import uuid7
from msdr.survey.scan_spectrum import SpectrumCapture
from msdr.survey.scan_storage import BandScanRepo


class BandScanError(RuntimeError):
    pass


def make_centers(
    start_hz: int, end_hz: int, span_hz: int, overlap: float, *, max_spans: int = 5_000
) -> list[int]:
    """Return span centers whose union covers [start_hz, end_hz]."""
    stride = max(1, round(span_hz * (1.0 - overlap)))
    low_center = start_hz + span_hz // 2
    high_center = end_hz - span_hz // 2
    if low_center >= high_center:
        return [(start_hz + end_hz) // 2]
    estimated = (high_center - low_center) // stride + 1
    if estimated + 1 > max_spans:
        raise BandScanError(
            f"range requires more than {max_spans} spans; increase span or decrease overlap"
        )
    centers = list(range(low_center, high_center + 1, stride))
    if centers[-1] != high_center:
        centers.append(high_center)
    return centers


class BandScan:
    """One persisted sweep; adapter ownership is mediated by injected callbacks."""

    def __init__(
        self,
        repo: BandScanRepo,
        tune_span: Callable,
        confirmed_span: Callable,
        labels: Callable,
        publish: Callable | None = None,
        snapshot: Callable | None = None,
    ):
        self.repo, self.tune_span, self.confirmed_span = repo, tune_span, confirmed_span
        self.labels, self.publish = labels, publish or (lambda _msg: None)
        self.snapshot = snapshot or (lambda: {"frame_seq": 0, "connected": True})
        self.current: dict | None = None
        self._task: asyncio.Task | None = None
        self._pause = asyncio.Event()
        self._pause.set()
        self._stop = asyncio.Event()
        self._last_persist_at = 0.0
        self._last_persisted_state: str | None = None
        self._result_index: dict[int, dict] = {}
        # Tolerance-bucketed results, keyed by int(frequency_hz // tolerance),
        # so a jittering label can be matched without scanning every result.
        self._result_buckets: dict[int, list[dict]] = {}
        self._result_seq: dict[int, int] = {}
        self._result_seq_counter = 0
        self._spectrum: SpectrumCapture | None = None

    def recover(self) -> None:
        self.repo.interrupt_unfinished()

    def status(self) -> dict:
        return {
            "state": self.current["state"] if self.current else "idle",
            "current": self._progress() if self.current else None,
            "history": self.repo.latest(),
        }

    def _progress(self) -> dict:
        current = self.current
        config = {key: value for key, value in current["config"].items() if key != "centers_hz"}
        spans = current["spans"]
        return {
            "id": current["id"],
            "state": current["state"],
            "config": config,
            "started_at": current["started_at"],
            "index": current["index"],
            "total": len(current["config"]["centers_hz"]),
            "completed_count": sum(span["state"] in {"covered", "uncovered"} for span in spans),
            "current_center_hz": spans[-1]["center_hz"] if spans else None,
            "covered_count": sum(span["state"] == "covered" for span in spans),
            "uncovered_count": sum(span["state"] == "uncovered" for span in spans),
            "result_count": len(current["results"]),
            "capture_degraded": self.repo.degraded_reason,
        }

    def start(
        self, start_hz: int, end_hz: int, span_hz: int, dwell_s: float, overlap: float
    ) -> dict:
        if self._task and not self._task.done():
            raise BandScanError("a band scan is already active")
        if start_hz <= 0 or end_hz <= start_hz:
            raise BandScanError("end frequency must be greater than start frequency")
        if span_hz <= 0 or dwell_s <= 0 or not 0 <= overlap < 1:
            raise BandScanError("span and dwell must be positive; overlap must be from 0 to <1")
        centers = make_centers(start_hz, end_hz, span_hz, overlap)
        config = {
            "start_hz": start_hz,
            "end_hz": end_hz,
            "span_hz": span_hz,
            "dwell_s": dwell_s,
            "overlap": overlap,
            "centers_hz": centers,
        }
        scan_id = uuid7()
        self.repo.create(scan_id, config)
        self._result_index = {}
        self._result_buckets = {}
        self._result_seq = {}
        self._result_seq_counter = 0
        self._spectrum = SpectrumCapture(start_hz, end_hz, native_bin_width_hz=span_hz / 512)
        self._last_persist_at = 0.0
        self._last_persisted_state = None
        self.current = {
            "id": scan_id,
            "state": "running",
            "config": config,
            "started_at": time.time(),
            "index": 0,
            "spans": [],
            "results": [],
        }
        self._pause.set()
        self._stop.clear()
        self._save()
        self._task = asyncio.create_task(self._run())
        return self.status()

    def pause(self) -> dict:
        self._need_active()
        self._pause.clear()
        self.current["state"] = "paused"
        self._save()
        return self.status()

    def resume(self) -> dict:
        self._need_active()
        self._pause.set()
        self.current["state"] = "running"
        self._save()
        return self.status()

    async def stop(self) -> dict:
        if not self._task:
            raise BandScanError("no band scan has run")
        if self._task.done():
            return self.status()
        self._stop.set()
        self._pause.set()
        if self._task:
            await self._task
        return self.status()

    def _need_active(self):
        if not self._task or self._task.done():
            raise BandScanError("no band scan is active")

    async def _run(self):
        current = self.current
        try:
            centers = current["config"]["centers_hz"]
            span = current["config"]["span_hz"]
            for index, center in enumerate(centers):
                # Final state for the previous span must be durable before a
                # later checkpoint can coalesce under writer pressure.
                if index:
                    await self.repo.aflush()
                await self._pause.wait()
                if self._stop.is_set():
                    break
                current["index"] = index
                item = {
                    "center_hz": center,
                    "start_hz": max(current["config"]["start_hz"], center - span // 2),
                    "end_hz": min(current["config"]["end_hz"], center + span // 2),
                    "state": "tuning",
                    "observed_at": time.time(),
                    "labels": [],
                    "visited": False,
                    "spectrum_captured": False,
                    "dsp_completed": False,
                    "dsp_result_count": 0,
                    "dsp_targets": 0,
                    "dsp_analyzed": 0,
                    "dsp_pending": 0,
                    "dsp_coverage_complete": None,
                    "usable_intervals_hz": [],
                }
                current["spans"].append(item)
                self._save(force=True)
                try:
                    report = await self.tune_span(span, center)
                    confirmed = self.confirmed_span()
                    ok = bool(report) and confirmed == (center, span, center)
                except Exception as exc:
                    item["state"] = "uncovered"
                    item["reason"] = f"tune failed: {exc}"
                    self._save(force=True)
                    continue
                if not ok:
                    item["state"] = "uncovered"
                    item["reason"] = "receiver did not confirm requested center and span"
                    self._save(force=True)
                    continue
                item["visited"] = True
                if self._stop.is_set():
                    item["state"] = "uncovered"
                    item["reason"] = "stopped before fresh capture"
                    self._save(force=True)
                    break
                baseline = self.snapshot()
                item["capture_started_at"] = time.time()
                initial_frame = baseline.get("frame_seq", 0)
                initial_generation = baseline.get("generation")
                initial_usable = baseline.get("usable_spectrum", 0)
                initial_dsp = baseline.get("dsp_completed", 0)
                item["state"] = "waiting_for_frame"
                self._save(force=True)
                frame_wait_remaining = 5.0
                while True:
                    await self._pause.wait()
                    if self._stop.is_set():
                        break
                    state = self.snapshot()
                    if (
                        initial_generation is not None
                        and state.get("generation") != initial_generation
                    ):
                        item["state"] = "uncovered"
                        item["reason"] = "Survey generation changed before capture"
                        break
                    if not state.get("connected", False) or self.confirmed_span() != (
                        center,
                        span,
                        center,
                    ):
                        item["state"] = "uncovered"
                        item["reason"] = "receiver disconnected or changed span before capture"
                        break
                    survey_span = (
                        state.get("survey_center_hz"),
                        state.get("survey_sample_rate_hz"),
                    )
                    frame_seq = state.get("frame_seq", 0)
                    if frame_seq > initial_frame and survey_span != (center, span):
                        item["state"] = "uncovered"
                        item["reason"] = "Survey processed a fresh frame at a different span"
                        break
                    if survey_span == (center, span) and frame_seq > initial_frame:
                        self._capture_frame(
                            item, state, initial_generation, initial_usable, initial_dsp
                        )
                        await self._record_labels(item, self.labels(), current["config"])
                        item["fresh_frames"] = frame_seq - initial_frame
                        accepted_frame = frame_seq
                        break
                    wait_started = time.monotonic()
                    await asyncio.sleep(0.05)
                    if self._pause.is_set() and not self._stop.is_set():
                        frame_wait_remaining -= time.monotonic() - wait_started
                    if frame_wait_remaining <= 0:
                        item["state"] = "uncovered"
                        item["reason"] = "no fresh spectrum frame received after confirmed tune"
                        break
                if item["state"] == "uncovered":
                    self._save(force=True)
                    if self._stop.is_set():
                        break
                    continue
                if self._stop.is_set():
                    item["state"] = "uncovered"
                    item["reason"] = "stopped before dwell"
                    self._save(force=True)
                    break
                item["state"] = "dwelling"
                self._save(force=True)
                remaining = current["config"]["dwell_s"]
                item["remaining_dwell_s"] = remaining
                last_frame = accepted_frame
                last_frame_at = time.monotonic()
                while remaining > 0 and not self._stop.is_set():
                    await self._pause.wait()
                    if self._stop.is_set():
                        break
                    state = self.snapshot()
                    if (
                        not state.get("connected", False)
                        or (
                            initial_generation is not None
                            and state.get("generation") != initial_generation
                        )
                        or self.confirmed_span() != (center, span, center)
                        or (state.get("survey_center_hz"), state.get("survey_sample_rate_hz"))
                        != (center, span)
                    ):
                        item["state"] = "uncovered"
                        item["reason"] = "receiver disconnected or changed span during capture"
                        break
                    interval = min(0.1, remaining)
                    before = time.monotonic()
                    await asyncio.sleep(interval)
                    elapsed = time.monotonic() - before
                    if self._pause.is_set() and not self._stop.is_set():
                        remaining = max(0.0, remaining - elapsed)
                        item["remaining_dwell_s"] = remaining
                    state = self.snapshot()
                    frame_seq = state.get("frame_seq", last_frame)
                    if self._pause.is_set() and frame_seq > last_frame:
                        self._capture_frame(
                            item, state, initial_generation, initial_usable, initial_dsp
                        )
                        await self._record_labels(item, self.labels(), current["config"])
                        item["fresh_frames"] = item.get("fresh_frames", 0) + frame_seq - last_frame
                        last_frame = frame_seq
                        last_frame_at = time.monotonic()
                        self._save()
                    elif self._pause.is_set() and time.monotonic() - last_frame_at > 2.0:
                        item["state"] = "uncovered"
                        item["reason"] = "spectrum frames stopped during dwell"
                        break
                if item["state"] == "uncovered":
                    self._save(force=True)
                    if self._stop.is_set():
                        break
                    continue
                if self._stop.is_set() and remaining > 0:
                    item["state"] = "uncovered"
                    item["reason"] = "stopped before dwell completed"
                    self._save(force=True)
                    break
                if remaining > 0:
                    item["state"] = "uncovered"
                    item["reason"] = "dwell did not complete"
                    self._save(force=True)
                    continue
                final_state = self.snapshot()
                if (
                    not final_state.get("connected", False)
                    or (
                        initial_generation is not None
                        and final_state.get("generation") != initial_generation
                    )
                    or self.confirmed_span() != (center, span, center)
                    or (
                        final_state.get("survey_center_hz"),
                        final_state.get("survey_sample_rate_hz"),
                    )
                    != (center, span)
                    or final_state.get("frame_seq", 0) <= initial_frame
                    or time.monotonic() - last_frame_at > 2.0
                ):
                    item["state"] = "uncovered"
                    item["reason"] = "receiver or fresh spectrum changed before dwell completed"
                    self._save(force=True)
                    continue
                final_seq = final_state.get("frame_seq", last_frame)
                if final_seq > last_frame:
                    self._capture_frame(
                        item, final_state, initial_generation, initial_usable, initial_dsp
                    )
                    await self._record_labels(item, self.labels(), current["config"])
                    item["fresh_frames"] = item.get("fresh_frames", 0) + final_seq - last_frame
                item["capture_ended_at"] = time.time()
                if "usable_spectrum" in final_state and not item["spectrum_captured"]:
                    item["state"] = "uncovered"
                    item["reason"] = "fresh frames had no usable spectrum samples"
                else:
                    item["state"] = "covered"
                self._save(force=True)
            current["state"] = "stopped" if self._stop.is_set() else "complete"
        except asyncio.CancelledError:
            current["state"] = "stopped"
            raise
        except Exception as exc:
            current["state"] = "failed"
            current["failure_reason"] = str(exc)
            if current["spans"] and current["spans"][-1]["state"] not in {"covered", "uncovered"}:
                current["spans"][-1]["state"] = "uncovered"
                current["spans"][-1]["reason"] = f"scan failed during capture: {exc}"
        finally:
            current["ended_at"] = time.time()
            spectrum_coverage = self._spectrum.export() if self._spectrum else None
            current["coverage_complete"] = (
                len(current["spans"]) == len(current["config"]["centers_hz"])
                and all(s.get("state") == "covered" for s in current["spans"])
                and bool(
                    spectrum_coverage
                    and spectrum_coverage["available"]
                    and not spectrum_coverage["holes"]
                )
            )
            try:
                self._save(force=True)
                await self.repo.aflush()
            except Exception as exc:
                current["state"] = "failed"
                current["failure_reason"] = f"scan storage did not flush: {exc}"
                current["capture_degraded"] = str(exc)
                try:
                    self._save(force=True)
                    await self.repo.aflush()
                except Exception:
                    pass
            self.publish({"type": "survey_band_scan", "status": self.status()})

    def _capture_frame(self, item: dict, state: dict, generation, initial_usable, initial_dsp):
        if generation is not None and state.get("generation") != generation:
            return
        if state.get("usable_spectrum", initial_usable) > initial_usable:
            spectrum = state.get("spectrum")
            if spectrum and self._spectrum:
                intervals = self._spectrum.add(spectrum)
                if intervals:
                    item["spectrum_captured"] = True
                    combined = item["usable_intervals_hz"] + intervals
                    combined.sort()
                    merged = []
                    for lo, hi in combined:
                        if merged and lo <= merged[-1][1]:
                            merged[-1][1] = max(merged[-1][1], hi)
                        else:
                            merged.append([lo, hi])
                    item["usable_intervals_hz"] = merged
        completed = state.get("dsp_completed", initial_dsp) - initial_dsp
        if completed > 0:
            item["dsp_completed"] = True
            item["dsp_result_count"] = max(item.get("dsp_result_count", 0), completed)
        if "dsp_targets" in state:
            item["dsp_targets"] = int(state.get("dsp_targets") or 0)
            item["dsp_analyzed"] = int(state.get("dsp_analyzed") or 0)
            item["dsp_pending"] = int(state.get("dsp_pending") or 0)
            item["dsp_coverage_complete"] = (
                item["dsp_targets"] > 0
                and item["dsp_pending"] == 0
                and item["dsp_analyzed"] >= item["dsp_targets"]
            )

    async def _record_labels(self, span: dict, labels: list[dict], config: dict):
        observations = self._merge_labels(span, labels, config)
        if observations:
            await self.repo.aappend_observations(self.current["id"], observations)

    def _find_by_tolerance(self, hz: int, tolerance: float) -> dict | None:
        """Nearest-bucket fallback for a label hz with no exact index hit.

        Checks only the bucket hz falls in plus its two neighbours (tolerance
        equals bucket width, so any in-range result must live in one of the
        three), instead of scanning every result. Ties are broken the same
        way the old linear scan did: the result created first wins.
        """
        bucket = int(hz // tolerance)
        best, best_seq = None, None
        for b in (bucket - 1, bucket, bucket + 1):
            for candidate in self._result_buckets.get(b, ()):
                if abs(candidate["frequency_hz"] - hz) <= tolerance:
                    seq = self._result_seq.get(id(candidate), 0)
                    if best is None or seq < best_seq:
                        best, best_seq = candidate, seq
        return best

    def _merge_labels(self, span: dict, labels: list[dict], config: dict):
        lo, hi = span["start_hz"], span["end_hz"]
        observations = []
        tolerance = min(2_000.0, config["span_hz"] / 1024)
        for label in labels or []:
            try:
                hz = int(round(label["frequency_hz"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not lo <= hz <= hi:
                continue
            last_seen = label.get("last_seen")
            capture_started = span.get("capture_started_at")
            if (
                not isinstance(last_seen, (int, float))
                or capture_started is None
                or last_seen < capture_started
            ):
                continue
            existing = self._result_index.get(hz)
            if existing is None:
                existing = self._find_by_tolerance(hz, tolerance)
            if existing is None:
                existing = {
                    "frequency_hz": hz,
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                    "observations": [],
                    "observation_count": 0,
                    "span_centers_hz": [],
                    "snr_min_db": None,
                    "snr_max_db": None,
                    "snr_sum_db": 0.0,
                    "snr_count": 0,
                }
                self.current["results"].append(existing)
                bucket = int(existing["frequency_hz"] // tolerance)
                self._result_buckets.setdefault(bucket, []).append(existing)
                self._result_seq[id(existing)] = self._result_seq_counter
                self._result_seq_counter += 1
            self._result_index[hz] = existing
            observed_at = time.time()
            existing["last_seen"] = observed_at
            if span["center_hz"] not in existing["span_centers_hz"]:
                existing["span_centers_hz"].append(span["center_hz"])
            sample = {
                "observed_at": observed_at,
                "span_center_hz": span["center_hz"],
                "span_start_hz": span["start_hz"],
                "span_end_hz": span["end_hz"],
                **{
                    k: label.get(k)
                    for k in (
                        "frequency_hz",
                        "snr_db",
                        "bw_hz",
                        "width_hz",
                        "spectrum_bandwidth_hz",
                        "bw_class",
                        "bw_source",
                        "activity",
                        "bandplan",
                        "allocation",
                        "catalog",
                        "family",
                        "modulation",
                        "evidence",
                        "confirmed",
                        "recognized",
                        "voice",
                        "rds",
                        "decoding",
                        "decoder",
                        "decoders",
                        "decoder_candidates",
                        "analysis_state",
                        "last_classification",
                        "last_seen",
                    )
                    if k in label
                },
            }
            # Normalize nearby label frequencies to the row key. Otherwise
            # updates at a slightly shifted measured peak split DB results.
            sample["frequency_hz"] = existing["frequency_hz"]
            observations.append(sample)
            existing["observation_count"] += 1
            snr = sample.get("snr_db")
            if isinstance(snr, (int, float)):
                existing["snr_min_db"] = (
                    snr if existing["snr_min_db"] is None else min(existing["snr_min_db"], snr)
                )
                existing["snr_max_db"] = (
                    snr if existing["snr_max_db"] is None else max(existing["snr_max_db"], snr)
                )
                existing["snr_sum_db"] += snr
                existing["snr_count"] += 1
            if "id" not in self.current:
                existing["observations"].append(sample)  # direct legacy unit usage
            else:
                reps = existing["observations"]
                # `or float("-inf")` would treat a 0.0 dB leader as having no
                # SNR at all, letting any later negative-SNR sample win.
                best_snr = reps[0].get("snr_db") if reps else None
                if reps and snr is not None and (best_snr is None or snr > best_snr):
                    reps[0] = sample
                elif not reps:
                    reps.append(sample)
                if len(reps) == 1:
                    reps.append(sample)
                else:
                    reps[-1] = sample
        return observations

    def _save(self, *, force=False):
        if self.current:
            state = self.current["state"]
            now = time.monotonic()
            if force or state != self._last_persisted_state or now - self._last_persist_at >= 1.0:
                current = self.current
                spans = current["spans"]
                meta = {k: v for k, v in current.items() if k not in {"results", "spans"}}
                meta.update(
                    result_count=len(current["results"]),
                    span_count=len(spans),
                    covered_count=sum(s["state"] == "covered" for s in spans),
                    uncovered_count=sum(s["state"] == "uncovered" for s in spans),
                )
                if self.repo.degraded_reason:
                    meta["capture_degraded"] = self.repo.degraded_reason
                spectrum = self._spectrum.export() if self._spectrum else None
                self.repo.checkpoint(
                    current["id"],
                    state,
                    meta,
                    spans[-1] if spans else None,
                    len(spans) - 1 if spans else None,
                    spectrum,
                )
                self._last_persisted_state = state
                self._last_persist_at = now
            self.publish(
                {
                    "type": "survey_band_scan",
                    "status": {"state": state, "current": self._progress()},
                }
            )
