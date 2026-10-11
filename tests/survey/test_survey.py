"""Survey: decimated spectrum pushes, 1/s survey status, occupancy reset on
span change, set_span ordering + VFO re-apply, feature-flag gate."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from msdr.engine.confirmed import StateChanged
from msdr.survey.peaks_spectrum import TrackedPeak
from msdr.survey.spectrum_hub import SpectrumHub
from msdr.survey.survey import Survey, SurveyError, _analysis_state_for_label


def test_analysis_state_tracks_real_results_and_rotation_freshness():
    frequency_hz = 162_430_000
    paged_out = _analysis_state_for_label(
        frequency_hz, 1000,
        {"active": True, "candidate_count": 40, "dwell_s": 2,
         "candidate_frequencies_hz": [frequency_hz], "targets": []},
        998.0, None, {"name": "Speech-like NFM"}, now_epoch=1000.0,
    )
    assert paged_out == {
        "state": "current_result", "at": 998.0, "refresh_pending": False,
    }

    # Scheduler bookkeeping alone is not a produced analyzer result.
    missing_result = _analysis_state_for_label(
        frequency_hz, 1000,
        {"active": True, "candidate_frequencies_hz": [frequency_hz],
         "targets": [{"frequency_hz": frequency_hz, "analyzed": True}]},
        None, None, None, now_epoch=1000.0,
    )
    assert missing_result["state"] == "waiting"

    old_result_waiting = _analysis_state_for_label(
        frequency_hz, 1000,
        {"active": True, "candidate_frequencies_hz": [frequency_hz],
         "analyzing": frequency_hz},
        700.0, {"name": "Data-like signal", "at": 700.0}, None,
        now_epoch=1000.0,
    )
    assert old_result_waiting == {
        "state": "waiting", "previous_at": 700.0,
        "previous_name": "Data-like signal",
    }

    historical = _analysis_state_for_label(
        frequency_hz, 1000, {"active": False}, 700.0,
        {"name": "Data-like signal", "at": 700.0}, None, now_epoch=1000.0,
    )
    assert historical == {
        "state": "historical_result", "at": 700.0,
        "name": "Data-like signal",
    }


def _survey(
    adapter,
    hub,
    settings,
    confirmed,
    published,
    *,
    feature_enabled=True,
    identify=None,
    allocation=None,
    iq_stream_factory=None,
    decode_start=None,
    decode_stop=None,
    on_labels_changed=None,
):
    return Survey(
        adapter=adapter,
        hub=hub,
        settings_getter=lambda: settings,
        publish=published.append,
        confirmed_state=confirmed,
        feature_enabled=lambda: feature_enabled,
        identify=identify,
        allocation=allocation,
        iq_stream_factory=iq_stream_factory,
        decode_start=decode_start,
        decode_stop=decode_stop,
        on_labels_changed=on_labels_changed,
    )


class _FakeIQStream:
    """Enough of IQStream's surface to let set_dimensions(["modulation"])
    pass -- these tests inject analyzer results directly via
    survey._on_analyzer_result rather than driving a real channelizer pass
    (same approach as tests/survey/test_survey_analyzer.py)."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def enable(self, on: bool) -> None:
        pass

    async def frames(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover -- makes this an async generator


def _peak_frame(bin_idx: int = 300, background: int = 50, level: int = 90) -> bytes:
    arr = bytearray([background] * 512)
    arr[bin_idx] = level
    return bytes(arr)


async def test_start_survey_raises_when_feature_disabled(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published, feature_enabled=False)
    await survey.start()
    with pytest.raises(SurveyError):
        await survey.start_survey()
    await survey.stop()


async def test_start_stop_survey_acquires_and_releases_hub(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    await survey.start_survey()
    assert hub.consumers == 1

    await survey.stop_survey()
    assert hub.consumers == 0

    await survey.stop()
    await hub.stop()


async def test_spectrum_pushes_are_decimated_to_settings_fps(
    adapter, confirmed, settings, published
):
    settings.spectrum_fps = 10  # min interval 0.1s
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    for _ in range(5):  # pushed back-to-back, well under one min-interval apart
        adapter.push_frame(bytes([80] * 512))
    await asyncio.sleep(0.05)

    spectrum_msgs = [m for m in published if m["type"] == "spectrum"]
    assert len(spectrum_msgs) == 1
    assert spectrum_msgs[0]["ref_level_db"] == -20
    assert spectrum_msgs[0]["base_db"] == -145

    await survey.stop()
    await hub.stop()


async def test_spectrum_markers_are_uncapped_and_show_dc_exclusion(
    adapter, confirmed, settings, published
):
    settings.center_exclusion_enabled = True
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    bins = bytearray([50] * 512)
    for index in range(5, 405, 5):
        bins[index] = 170
    bins[256] = 255  # receiver DC is excluded, even though it is tallest
    adapter.push_frame(bytes(bins))
    await asyncio.sleep(0.04)
    status = survey.status()
    markers = status["spectrum_markers"]
    assert len(status["labels"]) == 64
    assert len(markers) == 79
    assert all(m["peak_bin"] != 256 for m in markers)
    assert all(m["active_now"] for m in markers)
    assert all(m["peak_frequency_hz"] == round(
        153_000_000 - 8_000_000 / 2 + m["peak_bin"] * 8_000_000 / 512
    ) for m in markers)
    exclusion = status["peak_detection"]["center_exclusion"]
    assert exclusion["first_bin"] == 254 and exclusion["last_bin"] == 258
    assert exclusion["start_hz"] < 153_000_000 < exclusion["end_hz"]
    assert exclusion["reason"] == "receiver_dc"
    await survey.stop()
    await hub.stop()


async def test_center_peak_is_detected_by_default_and_exclusion_toggles_live(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    adapter.push_frame(_peak_frame(bin_idx=256, level=200))
    await asyncio.sleep(0.03)
    status = survey.status()
    assert status["peak_detection"]["center_exclusion_enabled"] is False
    assert status["peak_detection"]["center_exclusion"] is None
    assert status["peak_detection"]["marker_hold_s"] == 15.0
    assert [m["peak_bin"] for m in status["spectrum_markers"]] == [256]
    assert len(status["labels"]) == 1
    first_seen = status["spectrum_markers"][0]["last_seen"]
    await asyncio.sleep(0.02)
    later = survey.status()["spectrum_markers"][0]
    assert later["last_seen"] >= first_seen - 0.01
    assert later["last_seen"] <= first_seen + 0.01
    assert later["age_s"] >= status["spectrum_markers"][0]["age_s"]
    assert survey.scan_snapshot()["spectrum"]["center_exclusion_bins"] == -1

    settings.center_exclusion_enabled = True
    enabled = survey.status()
    assert enabled["peak_detection"]["center_exclusion_enabled"] is True
    assert enabled["peak_detection"]["center_exclusion"]["first_bin"] == 254
    assert enabled["spectrum_markers"] == []
    assert enabled["labels"] == []
    assert survey.scan_snapshot()["spectrum"]["center_exclusion_bins"] == 2
    adapter.push_frame(_peak_frame(bin_idx=256, level=200))
    await asyncio.sleep(0.03)
    assert survey.status()["spectrum_markers"] == []

    settings.center_exclusion_enabled = False
    adapter.push_frame(_peak_frame(bin_idx=256, level=200))
    await asyncio.sleep(0.03)
    assert [m["peak_bin"] for m in survey.status()["spectrum_markers"]] == [256]
    await survey.stop()
    await hub.stop()


async def test_peak_threshold_change_applies_while_survey_runs(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    adapter.push_frame(_peak_frame(bin_idx=300, level=90))
    await asyncio.sleep(0.05)
    assert len(survey._latest_peaks) == 1

    settings.peak_threshold_db = 25.0
    adapter.push_frame(_peak_frame(bin_idx=350, level=90))
    await asyncio.sleep(0.05)
    assert survey._tracker.threshold_units == round(25.0 * survey._units_per_db)
    # The old label may persist until its TTL, but the same-strength new
    # peak is no longer accepted at the higher threshold.
    assert len(survey._latest_peaks) == 1

    await survey.stop()
    await hub.stop()


async def test_set_span_resets_occupancy(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    frame = bytearray([80] * 512)
    frame[100] = 200
    for _ in range(5):
        adapter.push_frame(bytes(frame))
    await asyncio.sleep(0.05)
    assert any(v > 0 for v in survey.status()["occupancy"]["short"])

    await survey.set_span(4_000_000, 146_000_000, keep_vfo=False)
    status = survey.status()
    assert all(v == 0.0 for v in status["occupancy"]["short"])
    assert status["span"]["center_hz"] == 146_000_000
    assert status["span"]["sample_rate_hz"] == 4_000_000

    await survey.stop()
    await hub.stop()


async def test_external_center_change_resets_occupancy_and_updates_span(
    adapter, confirmed, settings, published
):
    # A change that came from ConfirmedState (operator retuned in SDRconnect's
    # own GUI, not via set_span) must reset occupancy the same way.
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    frame = bytearray([80] * 512)
    frame[50] = 200
    for _ in range(5):
        adapter.push_frame(bytes(frame))
    await asyncio.sleep(0.05)
    assert any(v > 0 for v in survey.status()["occupancy"]["short"])

    confirmed.emit(
        StateChanged("device_center_frequency", 153_000_000, 160_000_000, "external", 0.0)
    )
    await asyncio.sleep(0.02)

    status = survey.status()
    assert status["span"]["center_hz"] == 160_000_000
    assert all(v == 0.0 for v in status["occupancy"]["short"])

    await survey.stop()
    await hub.stop()


async def test_same_span_echoes_and_noop_set_do_not_reset_signal_rows(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)
    before = survey.status()
    assert before["labels"]
    label_id = before["labels"][0]["id"]
    generation = before["generation"]

    # ConfirmedState emits own echoes even when SDRconnect accepted a no-op
    # set. They cannot erase tracker ids or the UI's session rows.
    confirmed.emit(StateChanged("device_center_frequency", 153_000_000,
                                153_000_000, "own", 0.0))
    confirmed.emit(StateChanged("device_sample_rate", 8_000_000,
                                8_000_000, "own", 0.0))
    await asyncio.sleep(0.02)
    after_echoes = survey.status()
    assert after_echoes["generation"] == generation
    assert after_echoes["labels"][0]["id"] == label_id

    await survey.set_span(8_000_000, 153_000_000)
    after_set = survey.status()
    assert after_set["generation"] == generation
    assert after_set["labels"][0]["id"] == label_id
    await survey.stop()
    await hub.stop()


async def test_set_span_sets_rate_then_center_then_reapplies_vfo_in_range(
    adapter, confirmed, settings, published
):
    # Fixture starts at center=153_000_000, sample_rate=8_000_000, vfo=153_000_000.
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    await survey.set_span(2_000_000, 152_500_000, keep_vfo=True)

    # |153_000_000 - 152_500_000| = 500_000 <= 2_000_000/2 - 25_000 = 975_000 -> fits.
    assert adapter.sets == [
        ("device_sample_rate", 2_000_000),
        ("device_center_frequency", 152_500_000),
        ("device_vfo_frequency", 153_000_000),
    ]
    await survey.stop()


async def test_set_span_leaves_vfo_when_it_no_longer_fits(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    await survey.set_span(2_000_000, 200_000_000, keep_vfo=True)

    assert adapter.sets == [
        ("device_sample_rate", 2_000_000),
        ("device_center_frequency", 200_000_000),
    ]
    await survey.stop()


async def test_set_span_skips_vfo_when_keep_vfo_false(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    await survey.set_span(2_000_000, 152_500_000, keep_vfo=False)

    assert adapter.sets == [
        ("device_sample_rate", 2_000_000),
        ("device_center_frequency", 152_500_000),
    ]
    await survey.stop()


async def test_status_reports_presets_and_sample_rates(adapter, confirmed, settings, published):
    settings.presets = [
        {"name": "VHF 152 pagers", "sample_rate_hz": 2_000_000, "center_hz": 152_500_000}
    ]
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    status = survey.status()
    assert status["presets"] == settings.presets
    assert 8_000_000 in status["sample_rates_hz"]
    assert status["active"] is False
    assert status["span"]["center_hz"] == 153_000_000

    await survey.stop()


async def test_start_survey_reads_span_from_adapter_when_state_is_silent(settings, published):
    # ConfirmedState only reports changes; before anything moves the span must
    # still be known -- straight from the receiver's properties.
    from tests.survey.conftest import FakeAdapter, FakeConfirmed

    adapter = FakeAdapter(
        {
            "device_center_frequency": 153_891_260,
            "device_sample_rate": 8_000_000,
            "device_vfo_frequency": 152_840_000,
        }
    )
    confirmed = FakeConfirmed()
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    assert survey.status()["span"]["center_hz"] is None

    await survey.start_survey()
    assert survey.status()["span"] == {
        "center_hz": 153_891_260,
        "sample_rate_hz": 8_000_000,
        "vfo_hz": 152_840_000,
    }
    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_survey_labels_appear_in_status(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    status = survey.status()
    assert status["resolution_hz"] == 8_000_000 / 512
    assert status["dimensions"] == ["activity", "bandwidth", "bandplan", "catalog"]
    assert {d["id"] for d in status["available_dimensions"]} == {
        "activity",
        "bandwidth",
        "bandplan",
        "catalog",
        "modulation",
        "symbol_rate",
        "voice",
        "rds",
    }
    assert len(status["labels"]) == 1
    label = status["labels"][0]
    assert label["id"].startswith("p")
    assert label["activity"]["kind"] == "new"  # just seen, well under the 5s "new" window
    # 8 MS/s -> 15.6 kHz/bin: too coarse to call a single bin a carrier, so unresolved.
    assert label["bw_class"] == "unresolved"
    assert label["modulation"] is None
    assert label["symbol_rate_hz"] is None
    assert label["voice"] is None

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_dimension_filter_blanks_fields(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["activity"])

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    label = survey.status()["labels"][0]
    assert label["bw_class"] is None
    assert label["bandplan"] is None
    assert label["catalog"] == []
    assert label["activity"]["kind"] == "new"  # activity is always computed regardless of selection

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_set_dimensions_rejects_unknown(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    with pytest.raises(SurveyError):
        survey.set_dimensions(["not_a_real_dimension"])

    await survey.stop()


async def test_set_dimensions_rejects_needs_iq(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    with pytest.raises(SurveyError):
        survey.set_dimensions(["modulation"])

    await survey.stop()


async def test_label_carries_allocation_cached_per_band(adapter, confirmed, settings, published):
    calls = []

    def allocation(frequency_hz):
        calls.append(frequency_hz)
        return {
            "lo_hz": 0,
            "hi_hz": 1_000_000_000,
            "jurisdiction": "US",
            "status": "ok",
            "services": [{"name": "AMATEUR", "priority": "primary", "table": "non_federal"}],
        }

    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published, allocation=allocation)
    await survey.start()
    await survey.start_survey()

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    status1 = survey.status()
    status2 = survey.status()
    label = status1["labels"][0]
    assert label["allocation"]["services"][0]["name"] == "AMATEUR"
    assert label["bandplan"] is None  # no identify callback: the hint stays None
    assert status2["labels"][0]["allocation"] == label["allocation"]
    assert len(calls) == 1  # second status() reused the per-band cache

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_identify_cache_hit_within_ttl(adapter, confirmed, settings, published):
    calls = []

    def identify(frequency_hz, width_hz):
        calls.append((frequency_hz, width_hz))
        return [
            {
                "name": "Test Band",
                "category": "public-safety",
                "confidence": 0.8,
                "reason": "band plan hint",
                "provider": "bandplan",
            }
        ]

    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published, identify=identify)
    await survey.start()
    await survey.start_survey()

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    status1 = survey.status()
    status2 = survey.status()
    assert len(calls) == 1  # second status() call hit the 60s identify cache
    assert status1["labels"][0]["bandplan"]["name"] == "Test Band"
    assert status2["labels"][0]["bandplan"] == status1["labels"][0]["bandplan"]

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_analyzer_targets_orders_steady_duty_before_bursty(
    adapter, confirmed, settings, published
):
    # S3/S4 fix: a fixed dwell reliably captures real signal on a
    # steady/high-duty target (e.g. a repeater carrier), but a bursty one
    # (duty 0.0-0.3, the field's DMR/POCSAG traffic) needs several revisits
    # before the analyzer's activity gate has enough active audio -- analyse
    # the easy target first rather than spend early dwells on one that can't
    # produce a result yet. Same SNR on both, so only duty can be deciding
    # the order.
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()

    now = time.monotonic()
    units_per_db = survey._units_per_db
    snr_units = 20.0 * units_per_db

    steady = TrackedPeak(
        id="steady",
        bin_lo=100,
        bin_hi=100,
        centroid_bin=100.0,
        level=90,
        snr_units=snr_units,
        first_seen=now - 100.0,
        last_seen=now,
        on_runs=[],
        active_now=True,
        run_start=now - 100.0,  # active continuously for the whole window -> duty ~1.0
    )
    bursty = TrackedPeak(
        id="bursty",
        bin_lo=200,
        bin_hi=200,
        centroid_bin=200.0,
        level=90,
        snr_units=snr_units,
        first_seen=now - 100.0,
        last_seen=now - 29.0,
        on_runs=[(now - 90.0, now - 89.0), (now - 60.0, now - 59.0), (now - 30.0, now - 29.0)],
        active_now=False,
        run_start=0.0,
    )
    # Bursty listed first in _latest_peaks -- ordering must still promote
    # the steady one ahead of it.
    survey._latest_peaks = [bursty, steady]
    # Narrowband targets are only built when a narrowband IQ dimension is
    # selected (an "rds"-only selection must not queue narrowband dwells).
    survey._dimensions = [*survey._dimensions, "modulation"]

    targets = survey._analyzer_targets()

    resolution_hz = survey._sample_rate_hz / 512
    base_hz = survey._center_hz - survey._sample_rate_hz / 2
    steady_hz = int(round(base_hz + 100 * resolution_hz))  # FFT order: bin i at i*res
    bursty_hz = int(round(base_hz + 200 * resolution_hz))

    assert steady_hz in targets
    assert bursty_hz in targets
    assert targets.index(steady_hz) < targets.index(bursty_hz)
    await survey.stop()


async def test_tuned_vfo_precedes_stronger_peak_and_deduplicates_nearby_label(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    survey._dimensions = ["modulation", "voice"]
    survey._vfo_hz = 153_124_900  # near bin 264, but use exact receiver tuning
    now = time.monotonic()
    resolution_hz = survey._sample_rate_hz / 512
    near = TrackedPeak(
        id="near", bin_lo=264, bin_hi=264, centroid_bin=264.0,
        level=90, snr_units=12 * survey._units_per_db,
        first_seen=now - 1, last_seen=now, on_runs=[], active_now=True, run_start=now - 1,
    )
    strong = TrackedPeak(
        id="strong", bin_lo=320, bin_hi=320, centroid_bin=320.0,
        level=120, snr_units=30 * survey._units_per_db,
        first_seen=now - 60, last_seen=now, on_runs=[], active_now=True, run_start=now - 60,
    )
    survey._latest_peaks = [strong, near]
    targets = survey._analyzer_targets()
    assert targets[0] == 153_124_900
    assert len(targets) == 2
    assert targets[1] == int(round(153_000_000 - 4_000_000 + 320 * resolution_hz))

    survey._vfo_hz = 170_000_000  # outside the IQ span, so it cannot be analyzed
    assert 170_000_000 not in survey._analyzer_targets()
    await survey.stop()


async def test_analyzer_candidate_pool_includes_peaks_beyond_display_limit(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    survey._dimensions = ["modulation"]
    now = time.monotonic()
    survey._latest_peaks = [
        TrackedPeak(
            id=str(index), bin_lo=index, bin_hi=index, centroid_bin=float(index),
            level=90, snr_units=20 * survey._units_per_db,
            first_seen=now - 1, last_seen=now, on_runs=[], active_now=True,
            run_start=now - 1,
        )
        for index in range(100, 170)
    ]
    targets = survey._analyzer_targets()
    assert len(targets) == 71  # VFO plus all 70 peaks; display is capped at 64
    weakest_hz = int(round(
        survey._center_hz - survey._sample_rate_hz / 2 + 169 * survey._sample_rate_hz / 512
    ))
    assert weakest_hz in targets
    await survey.stop()


def _family_result() -> dict:
    return {
        "modulation": {"kind": "fm_fsk", "confidence": 0.85, "features": {}},
        "symbol_rate_hz": 1200.0,
        "family": {
            "name": "POCSAG1200 / AX.25 AFSK",
            "suggested_decoders": ["pocsag", "aprs"],
            "confidence": 0.5,
        },
        "voice": None,
        "analyzed_at": 12345.0,
    }


async def test_decode_uses_suggested_decoders_from_family(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    calls = []

    async def decode_start(frequency_hz, decoders):
        calls.append((frequency_hz, decoders))
        return {"band": {"channels": [{"frequency_hz": frequency_hz, "source": "pinned"}]}}

    survey = _survey(
        adapter,
        hub,
        settings,
        confirmed,
        published,
        iq_stream_factory=_FakeIQStream,
        decode_start=decode_start,
    )
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["modulation"])

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    frequency_hz = survey.status()["labels"][0]["frequency_hz"]
    survey._on_analyzer_result(frequency_hz, _family_result())

    result = await survey.decode(frequency_hz, None)

    assert calls == [(frequency_hz, ["pocsag", "aprs"])]
    assert result["band"]["channels"][0]["source"] == "pinned"
    assert survey.status()["labels"][0]["decoding"] is True

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_decode_raises_when_no_suggested_decoders(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()

    async def decode_start(frequency_hz, decoders):
        raise AssertionError("decode_start should not be called with no decoders available")

    survey = _survey(adapter, hub, settings, confirmed, published, decode_start=decode_start)
    await survey.start()
    await survey.start_survey()

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    frequency_hz = survey.status()["labels"][0]["frequency_hz"]  # no "modulation" dim -> no family
    with pytest.raises(SurveyError):
        await survey.decode(frequency_hz, None)

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_note_decode_event_confirms_label_and_bumps_family_confidence(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published, iq_stream_factory=_FakeIQStream)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["modulation"])

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    frequency_hz = survey.status()["labels"][0]["frequency_hz"]
    survey._on_analyzer_result(frequency_hz, _family_result())

    survey.note_decode_event(
        {
            "frequency_hz": frequency_hz,
            "decoder": "pocsag",
            "text": "ALPHA - Test Page",
            "ts": 1_700_000_000.0,
            "kind": "text",
            "fields": {},
            "integrity": "failed",
        }
    )

    label = survey.status()["labels"][0]
    assert label["confirmed"] == {
        "decoder": "pocsag",
        "sample": "ALPHA - Test Page",
        "at": 1_700_000_000.0,
        "events": 1,
        "integrity": "failed",
    }
    assert label["family"]["confidence"] == 1.0

    # A second event on the same label bumps the count but keeps the first sample.
    survey.note_decode_event(
        {"frequency_hz": frequency_hz, "decoder": "pocsag", "text": "second page"}
    )
    label = survey.status()["labels"][0]
    assert label["confirmed"]["events"] == 2
    assert label["confirmed"]["sample"] == "ALPHA - Test Page"

    # A later checked frame strengthens evidence and carries its own sample;
    # the first display sample stays stable for the label.
    survey.note_decode_event(
        {
            "frequency_hz": frequency_hz,
            "decoder": "pocsag",
            "text": "passing frame",
            "integrity": "passed",
        }
    )
    confirmation = survey.status()["labels"][0]["confirmed"]
    assert confirmation["integrity"] == "passed"
    assert confirmation["integrity_sample"] == "passing frame"
    assert confirmation["sample"] == "ALPHA - Test Page"

    # Review and stale replay output must not confirm a live label.
    before = confirmation["events"]
    survey.note_decode_event(
        {"frequency_hz": frequency_hz, "decoder": "pocsag", "analysis_run_id": "review-1"}
    )
    survey.note_decode_event(
        {"frequency_hz": frequency_hz, "decoder": "pocsag", "source_id": "replay:test.wav"}
    )
    assert survey.status()["labels"][0]["confirmed"]["events"] == before

    # During replay, only events from the active source and generation apply.
    survey._replay = SimpleNamespace(source_id="replay:test.wav", generation=4, status=lambda: {})
    survey.note_decode_event(
        {
            "frequency_hz": frequency_hz,
            "decoder": "pocsag",
            "source_id": "replay:test.wav",
            "generation": 3,
        }
    )
    assert survey.status()["labels"][0]["confirmed"]["events"] == before
    survey.note_decode_event(
        {
            "frequency_hz": frequency_hz,
            "decoder": "pocsag",
            "source_id": "replay:test.wav",
            "generation": 4,
        }
    )
    assert survey.status()["labels"][0]["confirmed"]["events"] == before + 1
    survey._replay = None

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_identity_summary_composes_name_decoders_and_notes(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published, iq_stream_factory=_FakeIQStream)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["activity", "bandwidth", "modulation"])

    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)

    frequency_hz = survey.status()["labels"][0]["frequency_hz"]
    survey._on_analyzer_result(frequency_hz, _family_result())

    summary = survey.identity_summary(frequency_hz)
    assert summary["name"] == "POCSAG1200 / AX.25 AFSK"
    assert summary["decoders"] == ["pocsag", "aprs"]
    assert "activity:" in summary["notes"]
    assert "modulation: fm_fsk" in summary["notes"]

    survey.note_decode_event(
        {"frequency_hz": frequency_hz, "decoder": "pocsag", "text": "ALPHA - Test Page", "ts": 1.0}
    )
    confirmed_summary = survey.identity_summary(frequency_hz)
    assert confirmed_summary["name"] == "POCSAG"
    assert confirmed_summary["decoders"] == ["pocsag"]

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_identity_summary_none_when_nothing_tracked(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    assert survey.identity_summary(153_000_000) is None

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_label_id_is_stable_while_the_centroid_jitters(
    adapter, confirmed, settings, published
):
    # Live 2026-09-26: ids derived from the centroid (p{kHz}) changed every few
    # seconds on wideband signals, so table rows vanished and reappeared.
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    adapter.push_frame(_peak_frame(bin_idx=300))
    await asyncio.sleep(0.05)
    [first] = survey.status()["labels"]
    adapter.push_frame(_peak_frame(bin_idx=301))
    await asyncio.sleep(0.05)
    [second] = survey.status()["labels"]
    assert second["frequency_hz"] != first["frequency_hz"]
    assert second["id"] == first["id"]
    await survey.stop()


async def test_clear_forgets_labels_and_bumps_generation(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    adapter.push_frame(_peak_frame())
    await asyncio.sleep(0.05)
    before = survey.status()
    assert len(before["labels"]) == 1

    after = survey.clear()
    assert after["labels"] == []
    assert after["generation"] == before["generation"] + 1
    assert after["active"] is True  # still surveying, just fresh
    await survey.stop()


async def test_replay_surveys_a_recording_then_returns_to_live(
    adapter, confirmed, settings, published, tmp_path
):
    import numpy as np

    from tests.survey.test_replay import _iq16, write_wav

    rate = 1_000_000
    path = tmp_path / "SDRconnect_IQ_x_100000000HZ.wav"
    write_wav(path, _iq16(rate, rate, 250_000, amp=0.5), rate)
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    status = await survey.start_replay(str(path))
    assert status["active"] is True
    assert status["replay"]["center_hz"] == 100_000_000
    assert status["span"]["sample_rate_hz"] == rate
    await asyncio.sleep(0.4)
    labels = survey.status()["labels"]
    assert any(abs(lb["frequency_hz"] - 100_250_000) < 5_000 for lb in labels)
    assert np.isfinite(survey.status()["replay"]["position_s"])
    with pytest.raises(SurveyError, match="replay"):
        await survey.set_span(2_000_000, 146_000_000)

    status = await survey.stop_replay()
    assert status["replay"] is None
    assert status["active"] is False
    await survey.stop()


async def test_recognized_label_outlives_label_ttl(adapter, confirmed, settings, published):
    settings.label_ttl_s = 0.3
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey._dimensions = [*survey._dimensions, "modulation"]
    frame = bytearray([50] * 512)
    frame[300] = frame[200] = 90
    adapter.push_frame(bytes(frame))
    await asyncio.sleep(0.05)
    labels = {round(lb["frequency_hz"]): lb for lb in survey.status()["labels"]}
    assert len(labels) == 2
    named_hz = max(labels)  # bin 300
    survey._on_analyzer_result(named_hz, _family_result())
    status = survey.status()  # this pass recognises it
    assert [
        lb["recognized"] for lb in sorted(status["labels"], key=lambda x: x["frequency_hz"])
    ] == [
        False,
        True,
    ]

    await asyncio.sleep(0.4)  # both unseen for > label_ttl_s
    adapter.push_frame(bytes([50] * 512))  # an empty frame: the tracker prunes
    await asyncio.sleep(0.05)
    remaining = survey.status()["labels"]
    assert [round(lb["frequency_hz"]) for lb in remaining] == [named_hz]
    assert remaining[0]["family"]["name"] == "POCSAG1200 / AX.25 AFSK"

    settings.recognized_ttl_s = 0.01  # a numeric cap still applies
    survey.status()
    adapter.push_frame(bytes([50] * 512))
    await asyncio.sleep(0.05)
    assert survey.status()["labels"] == []
    await survey.stop()


async def test_restart_forgets_confirmed_labels(adapter, confirmed, settings, published):
    # Review 2026-09-26: STOP -> START rebuilt the tracker (ids restart at t0)
    # but kept the confirms, so an old decode landed on an unrelated signal.
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey._confirmed_labels["p0"] = {"confirmed": {"decoder": "ais"}, "last_seen_mono": 0.0}
    await survey.stop_survey()
    await survey.start_survey()
    assert survey._confirmed_labels == {}
    await survey.stop()


async def test_on_labels_changed_fires_on_appear_and_retire_not_per_frame(
    adapter, confirmed, settings, published
):
    """S2 observations hook: fired at track-appear/retire boundaries, never
    from the per-frame pump loop (a steady, unchanged peak reprocessed frame
    after frame must not add extra calls)."""
    settings.label_ttl_s = 1.0
    hub = SpectrumHub(adapter)
    await hub.start()
    calls: list[list[dict]] = []
    survey = _survey(adapter, hub, settings, confirmed, published, on_labels_changed=calls.append)
    await survey.start()
    await survey.start_survey()

    adapter.push_frame(_peak_frame(), at=time.monotonic())
    await asyncio.sleep(0.05)
    assert len(calls) == 1  # track appears
    assert calls[0][0]["id"].startswith("p")

    for _ in range(3):
        adapter.push_frame(_peak_frame(), at=time.monotonic())
        await asyncio.sleep(0.01)
    assert len(calls) == 1  # same track re-measured -- no extra notify

    # An empty frame stamped far enough ahead that the track exceeds
    # label_ttl_s regardless of real scheduling jitter -- deterministic retire.
    adapter.push_frame(bytes([50] * 512), at=time.monotonic() + 10)
    await asyncio.sleep(0.05)
    assert len(calls) == 2  # track retires
    assert calls[1] == []

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_replay_span_follows_a_run_change(adapter, confirmed, settings, published, tmp_path):
    # S3: a multi-run recording retunes mid-replay; the survey's span must
    # follow the run (occupancy/tracker/identify reset), otherwise labels of
    # the old span would be read against the new frequencies.
    from tests.source.test_timeline import write_three_runs

    path = write_three_runs(tmp_path)
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    status = await survey.start_replay(str(path))
    assert status["span"]["center_hz"] == 100_000_000
    gen_before = status["generation"]
    replay = survey._replay
    replay.seek(replay.timeline.elapsed_s(2, 10))  # run 2: 101 MHz @ 20 kS/s
    for _ in range(50):
        await asyncio.sleep(0.05)
        if survey.status()["span"]["center_hz"] == 101_000_000:
            break
    st = survey.status()
    assert st["span"]["center_hz"] == 101_000_000
    assert st["span"]["sample_rate_hz"] == 20_000
    assert st["generation"] > gen_before
    assert st["replay"]["run_index"] == 2
    await survey.stop_replay()
    await survey.stop()


async def test_replay_seek_without_span_change_resets_tracked_state(
    adapter, confirmed, settings, published, tmp_path
):
    # Finding B regression: ReplaySource.seek() within a single run bumps the
    # generation and fires only a "generation" SourceEvent (no "run", since
    # centre/rate don't change) -- that must still clear tracks/occupancy/
    # identify-cache/generation the same as a span change, not be ignored.
    from tests.survey.test_replay import _iq16, write_wav

    rate = 1_000_000
    path = tmp_path / "SDRconnect_IQ_x_100000000HZ.wav"
    write_wav(path, _iq16(rate, rate, 250_000, amp=0.5), rate)
    hub = SpectrumHub(adapter)
    await hub.start()
    survey = _survey(adapter, hub, settings, confirmed, published)
    await survey.start()
    status = await survey.start_replay(str(path))
    gen_before = status["generation"]

    for _ in range(50):
        await asyncio.sleep(0.05)
        if survey.status()["labels"]:
            break
    assert survey.status()["labels"]  # the tone was tracked before the seek
    assert survey._known_track_ids  # tracker has live ids that must be lost

    replay = survey._replay
    replay.seek(0.0)  # same run, same span: only a "generation" event fires

    for _ in range(50):
        await asyncio.sleep(0.05)
        if survey.status()["generation"] > gen_before:
            break
    st = survey.status()
    assert st["generation"] > gen_before  # _reset_span_state ran
    assert st["span"]["center_hz"] == 100_000_000  # span itself is unchanged

    await survey.stop_replay()
    await survey.stop()
    await hub.stop()
