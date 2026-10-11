"""Retune boundaries reject queued spectrum and in-flight IQ results."""

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from msdr.engine.confirmed import StateChanged
from msdr.survey.analyzer import Analyzer
from msdr.survey.peaks_spectrum import TrackedPeak
from msdr.survey.spectrum_hub import SpectrumHub
from msdr.survey.survey import Survey


async def _survey(adapter, confirmed, settings):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = Survey(
        adapter=adapter,
        hub=hub,
        settings_getter=lambda: settings,
        publish=lambda _: None,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
    )
    await survey.start()
    await survey.start_survey()
    return survey, hub


async def test_retune_drops_queued_and_unsettled_spectrum_and_old_echoes(
    adapter, confirmed, settings
):
    survey, hub = await _survey(adapter, confirmed, settings)
    old_at = time.monotonic()
    adapter.push_frame(bytes([80] * 512), old_at)
    survey.begin_retune()
    generation = survey.scan_snapshot()["generation"]
    survey.end_retune(146_000_000, 4_000_000)
    confirmed.props.update(device_center_frequency=146_000_000, device_sample_rate=4_000_000)
    confirmed.emit(StateChanged("device_sample_rate", 8_000_000, 4_000_000, "own", 0.0))
    confirmed.emit(StateChanged("device_center_frequency", 153_000_000, 146_000_000, "own", 0.0))
    adapter.push_frame(bytes([80] * 512), old_at)
    adapter.push_frame(bytes([80] * 12), time.monotonic() + 1)
    await asyncio.sleep(0.02)
    snap = survey.scan_snapshot()
    assert snap["generation"] == generation
    assert snap["survey_center_hz"] == 146_000_000
    assert snap["usable_spectrum"] == 0
    assert snap["spectrum"] is None

    # An old queued event cannot undo the final span. A later real external
    # tune back to the old value is still accepted if ConfirmedState agrees.
    confirmed.emit(StateChanged("device_center_frequency", 146_000_000, 153_000_000, "own", 0.0))
    await asyncio.sleep(0.01)
    assert survey.scan_snapshot()["generation"] == generation

    await asyncio.sleep(0.26)
    adapter.push_frame(bytes([80] * 512))
    await asyncio.sleep(0.02)
    snap = survey.scan_snapshot()
    assert snap["usable_spectrum"] == 1
    assert snap["frame_seq"] == 1
    assert snap["spectrum"]["center_hz"] == 146_000_000
    assert snap["spectrum"]["center_exclusion_bins"] == -1
    assert abs(snap["spectrum"]["at"] - time.time()) < 1
    assert all(isinstance(v, float) for v in snap["spectrum"]["bins"])
    confirmed.props["device_center_frequency"] = 153_000_000
    confirmed.emit(
        StateChanged("device_center_frequency", 146_000_000, 153_000_000, "external", 0.0)
    )
    await asyncio.sleep(0.02)
    assert survey.scan_snapshot()["generation"] > generation
    assert survey.scan_snapshot()["survey_center_hz"] == 153_000_000
    await survey.stop()
    await hub.stop()


async def test_failed_set_span_releases_gate_at_partial_hardware_span(adapter, confirmed, settings):
    survey, hub = await _survey(adapter, confirmed, settings)
    original_set = adapter.set

    async def fail_center(prop, value):
        if prop == "device_center_frequency":
            raise ConnectionError("center write failed")
        await original_set(prop, value)

    adapter.set = fail_center
    with pytest.raises(ConnectionError, match="center write failed"):
        await survey.set_span(4_000_000, 146_000_000, keep_vfo=False)
    snap = survey.scan_snapshot()
    assert survey._retune_blocked is False
    assert snap["survey_center_hz"] == 153_000_000
    assert snap["survey_sample_rate_hz"] == 4_000_000
    await survey.stop()
    await hub.stop()


async def test_analyzer_inflight_result_and_obsolete_reset_are_discarded():
    results = []
    analyzer = Analyzer(
        iq_stream_factory=lambda: None,
        center_hz=100_000_000,
        sample_rate_hz=2_000_000,
        max_analyzed=2,
        dwell_s=0.1,
        min_snr_db=8.0,
        on_result=lambda f, fields: results.append((f, fields)),
        on_status=lambda: None,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_block(_iq):
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        return (100_100_000, {"analyzed_at": time.time()})

    analyzer._process_block = slow_block
    analyzer._queue.put_nowait(SimpleNamespace(data=b"\0" * 16, received_at=time.time()))
    analyzer._pump_task = asyncio.create_task(analyzer._pump())
    await entered.wait()
    analyzer.begin_retune()
    first_generation = analyzer._retune_gen
    stale = asyncio.create_task(
        analyzer.retune(101_000_000, 2_000_000, expected_generation=first_generation)
    )
    analyzer.begin_retune()
    current_generation = analyzer._retune_gen
    current = asyncio.create_task(
        analyzer.retune(102_000_000, 2_000_000, expected_generation=current_generation)
    )
    release.set()
    await asyncio.gather(stale, current)
    assert results == []
    assert analyzer._center_hz == 102_000_000
    assert analyzer._retune_blocked is False
    analyzer._pump_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await analyzer._pump_task


async def test_scan_labels_retains_more_than_display_cap_and_prioritizes_active(
    adapter, confirmed, settings
):
    survey, hub = await _survey(adapter, confirmed, settings)
    now = time.monotonic()
    survey._latest_peaks = [
        TrackedPeak(
            id=f"t{i}",
            bin_lo=5 + i * 5,
            bin_hi=5 + i * 5,
            centroid_bin=float(5 + i * 5),
            level=100,
            snr_units=100 - i,
            first_seen=now - 1,
            last_seen=now,
            active_now=i >= 70,
            run_start=now - 1,
        )
        for i in range(80)
    ]
    labels = survey.scan_labels()
    display = survey.status()["labels"]
    assert len(labels) == 80
    assert len(display) == 64
    assert {x["id"] for x in labels} == {f"p{i}" for i in range(80)}
    assert all(label["active_now"] for label in display[:10])
    assert {label["id"] for label in display[:10]} == {f"p{i}" for i in range(70, 80)}
    await survey.stop()
    await hub.stop()
