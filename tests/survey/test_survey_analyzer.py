"""Survey <-> Analyzer integration (S3/S4): set_dimensions starts/stops the
analyzer only when an iq_stream_factory exists, targets/results flow, and
results merge into/clear from labels. Uses a minimal fake IQ stream --
Analyzer's own pump/round-robin behaviour is tests/survey/test_analyzer.py's
job; this file is the Survey-side wiring around it."""

import asyncio
import dataclasses
import time

import pytest

import msdr.survey.survey as survey_module
from msdr.survey.rds import fm_channel_hz
from msdr.survey.spectrum_hub import SpectrumHub
from msdr.survey.survey import Survey, SurveyError


class FakeIQStream:
    """Enough of IQStream's surface for Analyzer.start()/stop(): frames()
    is an async generator that never yields (like a real stream with
    nothing arriving yet) -- these tests drive results via Survey's
    internal hook directly rather than through a real channelizer pass,
    since the merge/clear contract is Survey's, not Analyzer's."""

    def __init__(self):
        self.started = False
        self.enabled = None
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def enable(self, on: bool) -> None:
        self.enabled = on

    async def frames(self):
        await asyncio.Event().wait()  # blocks until the reader task is cancelled
        yield  # pragma: no cover -- makes this an async generator


def _survey_with_iq(adapter, hub, settings, confirmed, published):
    factory_calls = []

    def factory():
        stream = FakeIQStream()
        factory_calls.append(stream)
        return stream

    survey = Survey(
        adapter=adapter,
        hub=hub,
        settings_getter=lambda: settings,
        publish=published.append,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
        iq_stream_factory=factory,
    )
    return survey, factory_calls


def _peak_frame(bin_idx: int = 300, background: int = 50, level: int = 90) -> bytes:
    arr = bytearray([background] * 512)
    arr[bin_idx] = level
    return bytes(arr)


async def _wait_for_label(survey, *, timeout_s: float = 2.0) -> dict:
    """Poll for status()["labels"][0] instead of a fixed sleep -- a 50 ms
    sleep before reading it was flaky under full-suite load (review
    2026-09-28: IndexError, passed in isolation)."""
    deadline = time.monotonic() + timeout_s
    while True:
        labels = survey.status()["labels"]
        if labels:
            return labels[0]
        if time.monotonic() >= deadline:
            return survey.status()["labels"][0]  # let the assertion fail with a clear message
        await asyncio.sleep(0.005)


async def test_set_dimensions_accepts_needs_iq_when_factory_present(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()

    status = survey.set_dimensions(["modulation"])  # would 409 without a factory
    assert status["dimensions"] == ["modulation"]

    await survey.stop()


async def test_selecting_iq_dimension_starts_analyzer_with_fake_factory(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    survey, factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    survey.set_dimensions(["modulation"])
    await asyncio.sleep(0.02)  # let the background analyzer.start() task run

    assert len(factory_calls) == 1
    assert factory_calls[0].started is True
    assert survey.status()["analyzer"]["active"] is True

    await survey.stop()


async def test_deselecting_iq_dimensions_stops_analyzer(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    survey, factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()

    survey.set_dimensions(["modulation"])
    await asyncio.sleep(0.02)
    assert survey.status()["analyzer"]["active"] is True

    survey.set_dimensions([])
    await asyncio.sleep(0.02)

    assert survey.status()["analyzer"]["active"] is False
    assert factory_calls[0].stopped is True

    await survey.stop()


async def test_without_factory_needs_iq_dimensions_still_rejected(
    adapter, confirmed, settings, published
):
    # No iq_stream_factory passed at all -- exactly the pre-S3 constructor.
    hub = SpectrumHub(adapter)
    survey = Survey(
        adapter=adapter,
        hub=hub,
        settings_getter=lambda: settings,
        publish=published.append,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
    )
    await survey.start()

    with pytest.raises(SurveyError):
        survey.set_dimensions(["modulation"])

    await survey.stop()


async def test_analyzer_result_merges_into_matching_label(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["modulation", "symbol_rate"])
    await asyncio.sleep(0.02)

    adapter.push_frame(_peak_frame(bin_idx=300))
    label = await _wait_for_label(survey)
    frequency_hz = label["frequency_hz"]

    # Simulate an analyzer result for that same peak (Analyzer's own pump
    # is exercised in test_analyzer.py) -- within +-1 spectrum bin.
    survey._on_analyzer_result(
        frequency_hz,
        {
            "modulation": {
                "kind": "fm_fsk",
                "confidence": 0.85,
                "features": {
                    "env_cv": 0.1,
                    "ifreq_std_hz": 300.0,
                    "occ_bw_hz": 5000.0,
                    "symmetry": 0.02,
                },
            },
            "symbol_rate_hz": 1200.0,
            "family": {"name": "POCSAG1200 / AX.25 AFSK", "suggested_decoders": ["pocsag", "aprs"]},
            "voice": None,
            "analyzed_at": 12345.0,
        },
    )

    label = survey.status()["labels"][0]
    assert label["modulation"]["kind"] == "fm_fsk"
    assert label["symbol_rate_hz"] == 1200.0
    assert label["family"]["name"] == "POCSAG1200 / AX.25 AFSK"
    assert (
        next(c for c in label["decoder_candidates"] if c["id"] == "endoftrain")["evidence_kind"]
        == "iq_estimate"
    )
    assert label["analyzed_at"] == 12345.0

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_analyzer_result_cleared_when_label_leaves(
    adapter, confirmed, settings, published, monkeypatch
):
    settings.label_ttl_s = 0.08  # expires shortly after the checks below
    # A named family makes the label "recognised" (kept past label_ttl_s by
    # default); cap that too so this test is still about result pruning.
    settings.recognized_ttl_s = 0.08
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["modulation"])
    await asyncio.sleep(0.02)

    # _build_labels() prunes by `time.monotonic() - track.last_seen`, so the
    # "still fresh" checks below were racing real wall-clock time against an
    # 80ms label_ttl_s: _wait_for_label's poll loop (and everything after it,
    # up to the first status() call) had to complete in under 80ms of real
    # elapsed time, which isn't guaranteed under full-suite CPU contention --
    # the track would already be pruned, and `labels[0]` raised IndexError
    # (review 2026-09-28: "passed in isolation"). Freezing survey.py's clock
    # makes the elapsed time for that window exactly 0 regardless of how long
    # the surrounding awaits actually take, and lets the "ttl elapsed" check
    # advance the clock directly instead of sleeping and hoping.
    real_time = survey_module.time
    clock = {"t": real_time.monotonic()}

    class _FrozenClock:
        def monotonic(self):
            return clock["t"]

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(survey_module, "time", _FrozenClock())

    adapter.push_frame(_peak_frame(bin_idx=300), at=clock["t"])
    frequency_hz = (await _wait_for_label(survey))["frequency_hz"]

    survey._on_analyzer_result(
        frequency_hz,
        {
            "modulation": {"kind": "carrier", "confidence": 0.9, "features": {}},
            "symbol_rate_hz": None,
            "family": {"name": "carrier / beacon", "suggested_decoders": []},
            "voice": None,
            "analyzed_at": 1.0,
        },
    )
    assert survey.status()["labels"][0]["modulation"]["kind"] == "carrier"

    clock["t"] += 0.15  # label_ttl_s elapses, no new frames keep it alive
    assert survey.status()["labels"] == []
    assert survey._analyzer_results == []  # not matched on this pass -> pruned

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_completed_analysis_survives_brief_peak_dropout(
    adapter, confirmed, settings, published
):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["modulation"])
    adapter.push_frame(_peak_frame(bin_idx=300))
    frequency_hz = (await _wait_for_label(survey))["frequency_hz"]
    survey._on_analyzer_result(frequency_hz, {
        "modulation": {"kind": "fm_fsk", "confidence": 0.8, "features": {}},
        "symbol_rate_hz": None, "family": None, "voice": None,
        "analyzed_at": time.time(),
    })
    track = survey._latest_peaks[0]
    survey._latest_peaks = []  # one missing spectrum update, not a span change
    assert survey.status()["labels"] == []
    assert len(survey._analyzer_results) == 1
    survey._latest_peaks = [track]
    assert survey.status()["labels"][0]["modulation"]["kind"] == "fm_fsk"
    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


def test_adjacent_narrow_results_are_not_coalesced_at_wide_span():
    survey = object.__new__(Survey)
    survey._sample_rate_hz = 10_000_000  # one spectrum bin is 19.5 kHz
    survey._analyzer_results = []
    survey._rds_results = []
    survey._notify_labels_changed = lambda: None
    for frequency_hz, kind in ((162_430_000, "fm_fsk"), (162_442_500, "carrier")):
        survey._on_analyzer_result(frequency_hz, {
            "modulation": {"kind": kind}, "family": None, "voice": None,
            "analyzed_at": time.time(),
        })
    assert [r["frequency_hz"] for r in survey._analyzer_results] == [162_430_000, 162_442_500]


async def test_wide_label_gets_wideband_modulation_bypass(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    settings.dimensions = ["bandwidth", "modulation"]
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    await asyncio.sleep(0.02)

    # A wide (>25 kHz) peak at 8 MS/s / 512 bins (~15.6 kHz/bin): several
    # consecutive strong bins reads as wide_digital.
    arr = bytearray([50] * 512)
    for i in range(295, 310):
        arr[i] = 90
    adapter.push_frame(bytes(arr))
    label = await _wait_for_label(survey)
    assert label["bw_class"] in ("wfm", "wide_digital")
    assert label["modulation"] == {"kind": "wideband", "confidence": 0.5}
    assert label["symbol_rate_hz"] is None
    assert label["family"] is None

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


def _wide_frame() -> bytes:
    arr = bytearray([50] * 512)
    for i in range(295, 310):
        arr[i] = 90
    return bytes(arr)


async def test_rds_result_names_fm_broadcast_label(adapter, confirmed, settings, published):
    for props in (adapter.state, confirmed.props):
        props["device_center_frequency"] = 98_000_000
        props["device_vfo_frequency"] = 98_000_000
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["bandwidth", "rds"])  # rds alone starts the analyzer
    await asyncio.sleep(0.02)
    assert len(factory_calls) == 1

    adapter.push_frame(_wide_frame())
    label = await _wait_for_label(survey)
    frequency_hz = label["frequency_hz"]
    assert label["bw_class"] in ("wfm", "wide_digital")
    assert label["rds"] is None
    # The tuned channel (VFO 98.0 MHz) always comes first.
    assert survey._rds_targets() == [98_000_000, fm_channel_hz(frequency_hz)]
    assert survey._analyzer_targets() == []  # no narrowband dimension selected

    rds = {
        "pi": "0x1234",
        "ps": "KTEST",
        "pty": 10,
        "pty_name": "Country",
        "rt": "Now playing",
        "tp": False,
        "groups": 42,
        "block_error_rate": 0.01,
    }
    survey._on_analyzer_result(frequency_hz + 5_000, {"rds": rds, "analyzed_at": 111.0})

    label = survey.status()["labels"][0]
    assert label["rds"]["ps"] == "KTEST"
    assert label["family"]["name"] == "FM broadcast · KTEST"
    assert label["confirmed"] == {
        "decoder": "rds",
        "sample": "KTEST · Now playing",
        "at": 111.0,
        "events": 42,
    }
    assert survey.identity_summary(frequency_hz)["name"] == "FM broadcast · KTEST"

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_rds_result_survives_a_pass_without_its_label(
    adapter, confirmed, settings, published
):
    # Live bug 2026-09-26: a station's label dropped out for one 1 s rebuild and
    # its decoded name vanished until the next dwell on that channel (minutes).
    for props in (adapter.state, confirmed.props):
        props["device_center_frequency"] = 98_000_000
        props["device_vfo_frequency"] = 98_000_000
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, _calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["bandwidth", "rds"])
    await asyncio.sleep(0.02)
    adapter.push_frame(_wide_frame())
    frequency_hz = (await _wait_for_label(survey))["frequency_hz"]
    rds = {
        "pi": "0x1234",
        "ps": "KTEST",
        "pty": None,
        "pty_name": None,
        "rt": None,
        "tp": None,
        "groups": 12,
        "block_error_rate": 0.0,
    }
    survey._on_analyzer_result(frequency_hz, {"rds": rds, "analyzed_at": time.time()})
    assert survey.status()["labels"][0]["rds"]["ps"] == "KTEST"

    peaks = survey._latest_peaks
    survey._latest_peaks = []  # label missing for one pass
    assert survey.status()["labels"] == []
    survey._latest_peaks = peaks
    assert survey.status()["labels"][0]["rds"]["ps"] == "KTEST"

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_no_rds_targets_outside_fm_band(adapter, confirmed, settings, published):
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["rds"])
    await asyncio.sleep(0.02)

    adapter.push_frame(_wide_frame())  # same wide peak, but around 153 MHz
    await asyncio.sleep(0.05)
    assert survey.status()["labels"]
    assert survey._rds_targets() == []

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


async def test_rds_targets_one_per_fm_channel(adapter, confirmed, settings, published):
    for props in (adapter.state, confirmed.props):
        props["device_center_frequency"] = 98_000_000
        props["device_vfo_frequency"] = 98_000_000
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, _factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    survey.set_dimensions(["rds"])
    await asyncio.sleep(0.02)
    adapter.push_frame(_wide_frame())
    await asyncio.sleep(0.05)
    [main] = survey._latest_peaks
    vfo_hz, main_hz = survey._rds_targets()
    bin_hz = 8_000_000 / 512

    def peak_at(frequency_hz: float, snr_scale: float):
        centroid = (frequency_hz - (98_000_000 - 4_000_000)) / bin_hz - 0.5
        # The spectrum profile moves with the peak (RDS targets key on it).
        return dataclasses.replace(
            main,
            centroid_bin=centroid,
            profile_lo=main.profile_lo + round(centroid - main.centroid_bin),
            snr_units=main.snr_units * snr_scale,
        )

    survey._latest_peaks = [
        peak_at(main_hz + 43_000, 1.0),  # strongest: centroid off the carrier
        peak_at(main_hz - 60_000, 0.5),  # side peaks of the same station
        peak_at(main_hz + 70_000, 0.5),
        peak_at(main_hz + 200_000, 0.8),  # the neighbouring station
    ]
    assert survey._rds_targets() == [vfo_hz, main_hz, main_hz + 200_000]

    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


def test_station_hz_ignores_hd_radio_sidebands():
    from msdr.survey.peaks_spectrum import TrackedPeak

    # Live 2026-09-26, 19.5 kHz bins (dB above floor): KLLC 97.31 with its HD
    # sidebands merged into one label whose centroid sat at 97.39 (-> 97.4).
    profile = [12, 19, 21, 20, 20, 16, 16, 24, 30, 33, 36, 37, 36, 33, 30, 25,
               13, 15, 19, 19, 20, 17, 8]  # fmt: skip
    survey = object.__new__(Survey)
    survey._center_hz, survey._sample_rate_hz, survey._units_per_db = 100_310_000, 10_000_000, 1.0
    resolution_hz = 10_000_000 / 512
    lo = round((97_100_000 - (100_310_000 - 5_000_000)) / resolution_hz)
    track = TrackedPeak(
        id="t", bin_lo=lo, bin_hi=lo + len(profile) - 1, centroid_bin=lo + 15.0, level=37,
        snr_units=37.0, first_seen=0.0, last_seen=0.0, profile=profile, profile_lo=lo,
    )  # fmt: skip
    assert fm_channel_hz(survey._station_hz(track, 97_394_000, resolution_hz)) == 97_300_000
    track.profile = []
    assert survey._station_hz(track, 97_394_000, resolution_hz) == 97_394_000


async def test_persisted_iq_dimensions_start_the_analyzer_on_survey_start(
    adapter, confirmed, settings, published
):
    # Restart: IQ dimensions come back from settings at construction, while
    # the span is still unknown -- the analyzer must start with the survey.
    for props in (adapter.state, confirmed.props):
        props["device_center_frequency"] = 98_000_000
        props["device_vfo_frequency"] = 98_000_000
    settings.dimensions = ["bandwidth", "rds"]
    hub = SpectrumHub(adapter)
    await hub.start()
    survey, factory_calls = _survey_with_iq(adapter, hub, settings, confirmed, published)
    await survey.start()
    await survey.start_survey()
    await asyncio.sleep(0.02)
    assert len(factory_calls) == 1
    assert survey.status()["analyzer"]["active"] is True
    await survey.stop()


def test_newer_analysis_replaces_waiting_result_and_is_not_undone():
    from msdr.survey.survey import Survey

    survey = object.__new__(Survey)
    survey._sample_rate_hz = 10_000_000
    survey._analyzer_results = []
    survey._rds_results = []
    waiting = {"modulation": {"kind": "unknown"}, "analyzed_at": None, "note": "waiting for signal"}
    analyzed = {"modulation": {"kind": "fm_fsk"}, "analyzed_at": 123.0}
    survey._on_analyzer_result(144_397_891, waiting)
    survey._on_analyzer_result(144_397_891, analyzed)
    assert [r["fields"] for r in survey._analyzer_results] == [analyzed]
    # A later empty dwell keeps the analysis.
    survey._on_analyzer_result(144_398_000, waiting)
    assert [r["fields"] for r in survey._analyzer_results] == [analyzed]


def test_last_meaningful_classification_survives_unknown_without_crossing_channels():
    survey = object.__new__(Survey)
    survey._sample_rate_hz = 6_000_000
    survey._analyzer_results = []
    survey._rds_results = []
    survey._on_analyzer_result(160815000, {
        "family": {"name": "Speech-like NFM"}, "analyzed_at": 123.0,
    })
    survey._on_analyzer_result(160815000, {"family": None, "analyzed_at": 124.0})
    result = survey._analyzer_results[0]["fields"]
    assert result["family"] is None
    assert result["last_classification"]["name"] == "Speech-like NFM"
    assert result["last_classification"]["at"] == 123.0
    survey._on_analyzer_result(160800000, {"family": None, "analyzed_at": 125.0})
    assert "last_classification" not in survey._analyzer_results[-1]["fields"]
