import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.rfml.classifier import RFMLClassifier
from msdr.settings import RFMLSettings
from msdr.survey.analyzer import Analyzer
from msdr.survey.peaks_spectrum import TrackedPeak
from msdr.survey.spectrum_hub import SpectrumHub
from msdr.survey.survey import Survey
from tests.survey.conftest import FakeAdapter, FakeConfirmed, FakeSurveySettings


class FakeML:
    def __init__(self):
        self.invalidations = 0
        self.submissions = []

    def invalidate(self):
        self.invalidations += 1

    def submit(self, samples, rate, metadata):
        self.submissions.append((samples, rate, metadata))

    def status(self):
        return {"state": "fake"}

    async def start(self):
        pass

    async def stop(self):
        pass


def _analyzer(ml_settings=None):
    return Analyzer(
        iq_stream_factory=lambda: None,
        center_hz=100_000_000,
        sample_rate_hz=1_000_000,
        max_analyzed=1,
        dwell_s=2.0,
        min_snr_db=0,
        on_result=lambda *_: None,
        on_status=lambda: None,
        ml_settings=ml_settings,
    )


def _survey_fixture():
    adapter = FakeAdapter(
        {
            "device_center_frequency": 153_000_000,
            "device_sample_rate": 8_000_000,
            "device_vfo_frequency": 153_000_000,
            "spectrum_ref_level": -20,
            "spectrum_base": -145,
        }
    )
    confirmed = FakeConfirmed(
        {
            "device_center_frequency": 153_000_000,
            "device_sample_rate": 8_000_000,
            "device_vfo_frequency": 153_000_000,
        }
    )
    return adapter, confirmed, FakeSurveySettings(), []


@pytest.mark.asyncio
async def test_disabled_rfml_preserves_dsp_result_and_enabled_uses_contiguous_window():
    block = np.concatenate(
        (
            np.ones(4096, dtype=np.complex64),
            np.full(4096, 3 + 2j, dtype=np.complex64),
        )
    )

    def configured(settings):
        analyzer = _analyzer(settings)
        analyzer._channelizer = SimpleNamespace(process=lambda _: np.ones((1, 1)))
        analyzer._current_k = 0
        analyzer._narrow = SimpleNamespace(process=lambda _: block.copy(), out_rate=4096)
        analyzer._analyze_target = lambda buf, rate: (123_456, {"family": "test"})
        return analyzer

    baseline = configured(None)
    disabled = configured(RFMLSettings(enabled=False))
    enabled = configured(RFMLSettings(enabled=True))

    baseline_result = baseline._process_block(np.zeros(1, dtype=np.complex64))
    disabled_result = disabled._process_block(np.zeros(1, dtype=np.complex64))
    enabled_result = enabled._process_block(np.zeros(1, dtype=np.complex64))

    assert baseline_result == disabled_result
    assert enabled_result[0] == baseline_result[0]
    assert {k: v for k, v in enabled_result[1].items() if k != "_ml_window"} == baseline_result[1]
    window, rate = enabled_result[1]["_ml_window"]
    assert rate == 4096
    assert len(window) == 8192
    np.testing.assert_array_equal(window, block)


@pytest.mark.asyncio
async def test_analyzer_drop_and_source_generation_invalidate_ml_work():
    analyzer = _analyzer()
    fake_ml = FakeML()
    analyzer._ml = fake_ml
    analyzer._dropped = 1
    analyzer._process_block = lambda _: None
    frame = SimpleNamespace(
        data=np.zeros(8192, dtype="<i2").tobytes(),
        generation=None,
        sample_index=0,
        run_index=0,
    )
    analyzer._queue.put_nowait(frame)
    pump = asyncio.create_task(analyzer._pump())
    try:
        await asyncio.wait_for(_until(lambda: fake_ml.invalidations == 1), 1)
    finally:
        pump.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pump

    analyzer._source_generation = 10
    analyzer._invalidate_source_generation(11)
    assert fake_ml.invalidations == 2
    analyzer._invalidate_source_generation(11)  # duplicate generation is inert
    assert fake_ml.invalidations == 2


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0)


def test_analyzer_rejects_delayed_result_from_retuned_epoch_or_removed_target():
    analyzer = _analyzer()
    analyzer._ml = FakeML()
    analyzer._retune_gen = 4
    analyzer.set_targets([100_000_000])
    received = []
    analyzer._on_ml_result = lambda metadata, result: received.append((metadata, result))

    analyzer._accept_ml_result(
        {"analyzer_epoch": 3, "frequency_hz": 100_000_000}, {"backend": "fake"}
    )
    analyzer._accept_ml_result(
        {"analyzer_epoch": 4, "frequency_hz": 100_000_000}, {"backend": "fake"}
    )
    assert len(received) == 1
    assert received[0][0]["analyzer_epoch"] == 4
    analyzer.set_targets([])
    analyzer._accept_ml_result(
        {"analyzer_epoch": 4, "frequency_hz": 100_000_000}, {"backend": "fake"}
    )
    assert len(received) == 1


def test_survey_rejects_delayed_results_for_retired_track_and_old_generation():
    adapter, confirmed, settings, published = _survey_fixture()
    survey = Survey(
        adapter=adapter,
        hub=SpectrumHub(adapter),
        settings_getter=lambda: settings,
        publish=published.append,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
    )
    survey._center_hz = 100_000_000
    survey._sample_rate_hz = 1_000_000
    survey._generation = 7
    survey._latest_peaks = [TrackedPeak("t4", 255, 257, 256, 90, 40, 0, 0, profile=[90])]
    freq = 100_000_000 - 500_000 + 256 * (1_000_000 / 512)
    metadata = survey._ml_context(freq)
    assert {key: metadata[key] for key in ("candidate_id", "survey_generation", "source_id")} == {
        "candidate_id": "p4",
        "survey_generation": 7,
        "source_id": "live:sdrconnect",
    }
    assert metadata["receiver_center_hz"] == 100_000_000
    assert metadata["spectrum_bandwidth_hz"] > 0

    survey._on_ml_result(metadata, {"classification": "FM"})
    assert survey._rfml_results == {"p4": {"classification": "FM"}}
    survey._latest_peaks = []  # track retired while prediction was in flight
    survey._on_ml_result(metadata, {"classification": "PSK"})
    assert survey._rfml_results == {"p4": {"classification": "FM"}}
    survey._latest_peaks = [TrackedPeak("t4", 255, 257, 256, 90, 40, 0, 0)]
    survey._generation += 1
    survey._on_ml_result(metadata, {"classification": "PSK"})
    assert survey._rfml_results == {"p4": {"classification": "FM"}}


@dataclass
class FakeIQStream:
    started: bool = False
    stopped: bool = False

    async def start(self):
        self.started = True

    async def enable(self, _enabled):
        pass

    async def stop(self):
        self.stopped = True

    async def frames(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_missing_optional_dependency_keeps_normal_survey_running(monkeypatch):
    adapter, confirmed, settings, published = _survey_fixture()
    settings.dimensions = ["modulation"]
    settings.ml = RFMLSettings(enabled=True)

    def no_torch(_self):
        raise ImportError("torch is not installed")

    monkeypatch.setattr(RFMLClassifier, "load", no_torch)
    hub = SpectrumHub(adapter)
    await hub.start()
    stream = FakeIQStream()
    survey = Survey(
        adapter=adapter,
        hub=hub,
        settings_getter=lambda: settings,
        publish=published.append,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
        iq_stream_factory=lambda: stream,
    )
    await survey.start()
    await survey.start_survey()
    await asyncio.sleep(0.03)
    assert stream.started
    assert survey.status()["analyzer"]["rfml"]["state"] == "unavailable"

    bins = bytearray([50] * 512)
    bins[300] = 90
    adapter.push_frame(bytes(bins))
    await asyncio.sleep(0.05)
    assert survey.status()["labels"]
    assert survey.status()["analyzer"]["active"]
    await survey.stop_survey()
    await survey.stop()
    await hub.stop()


def test_live_detector_contiguous_windows_invalidation_and_expiry():
    from msdr.settings import RFMLDetectorSettings

    analyzer = _analyzer()
    analyzer._ml_detector = FakeML()
    analyzer._ml_detector_settings = RFMLDetectorSettings(window_duration_ms=4.096)
    analyzer._clock = lambda: 10
    frame = SimpleNamespace(generation=1, run_index=0)
    analyzer._submit_detector_frame(np.ones(2048, np.complex64), frame)
    assert not analyzer._ml_detector.submissions
    analyzer._submit_detector_frame(np.full(2048, 2j, np.complex64), frame)
    samples, rate, metadata = analyzer._ml_detector.submissions[0]
    assert rate == 1_000_000
    np.testing.assert_array_equal(samples[:2048], 1)
    np.testing.assert_array_equal(samples[2048:], 2j)
    analyzer._accept_detector_result(metadata, {"candidates": []})
    assert analyzer._detector_status()["result"] is not None
    analyzer._clock = lambda: 16
    assert analyzer._detector_status()["result"] is not None
    assert analyzer._detector_status()["result_stale"] is True
    analyzer._clock = lambda: 41
    assert analyzer._detector_status()["result"] is None
    analyzer._submit_detector_frame(np.ones(2048, np.complex64), frame)
    analyzer.invalidate_ml()
    assert analyzer._ml_detector_samples == 0
    assert analyzer._ml_detector.invalidations == 1
    assert analyzer._detector_status()["result"] is None
    analyzer._accept_detector_result({"analyzer_epoch": -1}, {"candidates": []})
    assert analyzer._detector_status()["result"] is None


def test_detector_skips_copy_while_throttled_and_reports_temporal_coverage():
    from msdr.settings import RFMLDetectorSettings

    analyzer = _analyzer()
    detector = FakeML()
    detector.ready = False
    detector.can_accept = lambda: detector.ready
    analyzer._ml_detector = detector
    analyzer._ml_detector_settings = RFMLDetectorSettings(window_duration_ms=4.096)
    analyzer._clock = lambda: 10
    analyzer._targets = [100_001_000]
    frame = SimpleNamespace(generation=1, run_index=0)
    block = np.ones(2048, np.complex64)
    analyzer._submit_detector_frame(block, frame)
    assert analyzer._ml_detector_parts == []
    assert analyzer._ml_detector_samples == 0
    detector.ready = True
    analyzer._submit_detector_frame(block, frame)
    analyzer._submit_detector_frame(block, frame)
    assert len(detector.submissions) == 1
    _, _, metadata = detector.submissions[0]
    assert metadata["focus_frequencies_hz"] == [100_001_000]
    assert metadata["capture_monotonic"] == 10
    status = analyzer._detector_status()
    assert status["input_samples"] == 6144
    assert status["sampled_samples"] == 4096
    assert status["accepted_samples"] == 4096
    assert status["sample_coverage"] == pytest.approx(2 / 3)
    assert status["missed_temporal_fraction"] == pytest.approx(1.0)
    assert status["skipped_input_fraction"] == pytest.approx(1 / 3)
    analyzer.invalidate_ml()
    assert analyzer._detector_status()["sample_coverage"] is None


def test_detector_window_defaults_to_100ms_and_scales_with_input_rate():
    from msdr.settings import RFMLDetectorSettings

    settings = RFMLDetectorSettings()
    assert settings.window_duration_ms == 100.0
    # Old saved sample windows had no reliable timebase. Drop them and use the
    # stable duration unless the new duration field was explicitly saved.
    migrated = RFMLDetectorSettings.model_validate({"window_samples": 65536})
    assert migrated.window_duration_ms == 100.0
    explicit = RFMLDetectorSettings.model_validate({
        "window_samples": 65536, "window_duration_ms": 80.0,
    })
    assert explicit.window_duration_ms == 80.0

    analyzer = _analyzer()
    analyzer._ml_detector = FakeML()
    analyzer._ml_detector_settings = settings
    analyzer._sample_rate_hz = 2_000_000
    frame = SimpleNamespace(generation=1, run_index=0)
    analyzer._submit_detector_frame(np.ones(199_999, np.complex64), frame)
    assert not analyzer._ml_detector.submissions
    analyzer._submit_detector_frame(np.ones(1, np.complex64), frame)
    samples, rate, metadata = analyzer._ml_detector.submissions[0]
    assert len(samples) == 200_000
    assert rate == 2_000_000
    assert metadata["capture_duration_s"] == pytest.approx(0.1)


@pytest.mark.parametrize("rate", [6_000_000, 10_000_000])
def test_detector_windows_retain_100ms_at_wideband_rates(rate):
    from msdr.settings import RFMLDetectorSettings

    analyzer = _analyzer()
    analyzer._sample_rate_hz = rate
    analyzer._ml_detector = FakeML()
    analyzer._ml_detector_settings = RFMLDetectorSettings()
    analyzer._submit_detector_frame(np.ones(rate // 10, np.complex64), SimpleNamespace())
    samples, submitted_rate, metadata = analyzer._ml_detector.submissions[0]
    assert len(samples) == rate // 10
    assert submitted_rate == rate
    assert metadata["capture_duration_s"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_configure_ml_applies_live_detector_settings_without_dsp_reset():
    from msdr.settings import RFMLDetectorSettings

    analyzer = _analyzer()
    analyzer._channelizer = object()
    channelizer = analyzer._channelizer
    analyzer._frames = 12
    analyzer._retune_gen = 3
    original = RFMLSettings(detector=RFMLDetectorSettings(enabled=True, interval_s=2.0))
    await analyzer.configure_ml(original)
    worker = analyzer._ml_detector
    analyzer._ml_detector_parts = [np.ones(7, np.complex64)]
    analyzer._ml_detector_samples = 7
    analyzer._ml_detector_input_samples = 20
    worker._started = True
    worker._last_submit = time.monotonic()
    assert not worker.can_accept()

    updated = RFMLSettings(detector=RFMLDetectorSettings(enabled=True, interval_s=0.5))
    await analyzer.configure_ml(updated)
    assert analyzer._ml_detector is worker
    assert worker.settings is updated.detector
    assert worker.settings.interval_s == 0.5
    assert worker.can_accept()
    assert analyzer._ml_detector_parts == []
    assert analyzer._ml_detector_samples == 0
    assert analyzer._ml_detector_input_samples == 0

    resized = RFMLSettings(detector=RFMLDetectorSettings(
        enabled=True, interval_s=0.5, window_duration_ms=150.0,
    ))
    await analyzer.configure_ml(resized)
    assert analyzer._ml_detector is worker
    assert worker.settings.window_duration_ms == 150.0
    assert analyzer._ml_detector_parts == []
    assert analyzer._ml_detector_samples == 0
    assert analyzer._ml_detector_input_samples == 0
    assert analyzer._channelizer is channelizer
    assert analyzer._frames == 12
    assert analyzer._retune_gen == 3


@pytest.mark.asyncio
async def test_detector_rejects_gap_after_first_frame_of_new_generation():
    from msdr.settings import RFMLDetectorSettings

    analyzer = _analyzer()
    analyzer._ml_detector = FakeML()
    analyzer._ml_detector_settings = RFMLDetectorSettings(window_duration_ms=4.096)
    analyzer._source = SimpleNamespace(descriptor=lambda: SimpleNamespace(
        generation=1, center_hz=100_000_000, sample_rate_hz=1_000_000,
    ))
    analyzer._source_generation = 1
    analyzer._processed_source_generation = 0
    analyzer._process_block = lambda _: None
    def enqueue(index):
        analyzer._queue.put_nowait(SimpleNamespace(
            data=np.ones(4096, dtype="<i2").tobytes(),
            generation=1, sample_index=index, run_index=0,
        ))
    enqueue(0)
    pump = asyncio.create_task(analyzer._pump())
    try:
        await asyncio.wait_for(_until(lambda: analyzer._ml_detector_samples == 2048), 1)
        enqueue(4096)  # 2048 samples per frame: skip 2048 after the first.
        await asyncio.wait_for(_until(lambda: analyzer._frames == 2), 1)
        assert analyzer._ml_detector.submissions == []
        assert analyzer._ml_detector_samples == 2048
        assert analyzer._ml_detector.invalidations == 2
    finally:
        pump.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pump


def test_gapped_dwell_keeps_dsp_but_does_not_emit_ml_window():
    analyzer = _analyzer(RFMLSettings(enabled=True))
    analyzer._channelizer = SimpleNamespace(process=lambda _: np.ones((1, 1)))
    analyzer._current_k = 0
    analyzer._narrow = SimpleNamespace(
        process=lambda _: np.ones(8192, dtype=np.complex64), out_rate=4096
    )
    analyzer._analyze_target = lambda buf, rate: (123456, {"family": "test"})
    analyzer._accum_has_gap = True
    assert analyzer._process_block(np.zeros(1, dtype=np.complex64)) == (
        123456, {"family": "test"}
    )
