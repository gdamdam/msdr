import numpy as np
import pytest

from msdr.source.inspection import CarrierDriftTracker, InspectionBuffer, ReceiverInspection


def test_scope_is_bounded_peak_preserving_and_tracks_clock_gaps():
    scope = InspectionBuffer("audio", max_samples=100)
    x = np.zeros(150, dtype=np.float32)
    x[97] = 1.0
    scope.append(x, sample_rate_hz=1000, sample_index=20, generation=2, session_id="a")
    snap = scope.snapshot(max_points=20, window_s=0.08).as_dict()
    assert len(snap["values"]) <= 20
    assert max(snap["max_values"]) == 1.0
    assert snap["sample_index"] == 90
    scope.append(np.ones(4), sample_rate_hz=1000, sample_index=200, generation=2, session_id="a")
    snap = scope.snapshot().as_dict()
    assert snap["gap_count"] == 1
    assert snap["last_gap"] == {
        "expected_sample_index": 170,
        "received_sample_index": 200,
        "missing_samples": 30,
    }
    assert snap["session_id"] == "a"


def test_trigger_snapshot_centers_on_a_crossing_when_post_trigger_data_exists():
    scope = InspectionBuffer("audio")
    scope.configure(trigger_level=0.0, trigger_mode="rising")
    x = np.tile(np.array([-1.0, 1.0], dtype=np.float32), 100)
    scope.append(x, sample_rate_hz=1000, sample_index=0, generation=0)
    snap = scope.snapshot(window_s=0.05, max_points=50).as_dict()
    assert snap["trigger_status"] == "triggered"
    assert snap["trigger_sample_index"] is not None
    assert len(snap["time_s"]) == len(snap["min_values"]) == len(snap["max_values"])
    assert len(snap["time_s"]) == 50


def test_drift_tracker_gates_unsupported_and_corrects_a_copy():
    tracker = CarrierDriftTracker(max_offset_hz=200.0, min_rms=0.01)
    fs, offset = 10_000.0, 125.0
    t = np.arange(2000) / fs
    tone = (0.5 * np.exp(2j * np.pi * offset * t)).astype(np.complex64)
    rejected = tracker.update(
        tone, sample_rate_hz=fs, sample_index=0, generation=0, signal_kind="meshtastic"
    )
    assert not rejected.accepted and rejected.reason == "unsupported_signal"
    reading = tracker.update(
        tone, sample_rate_hz=fs, sample_index=0, generation=0, signal_kind="unmodulated_carrier"
    )
    assert reading.offset_hz == pytest.approx(offset, abs=0.01)
    corrected = tracker.correct(tone, sample_rate_hz=fs)
    assert corrected is not tone
    assert np.array_equal(tone, (0.5 * np.exp(2j * np.pi * offset * t)).astype(np.complex64))
    phase_step = np.angle(np.sum(corrected[1:] * np.conj(corrected[:-1])))
    assert phase_step == pytest.approx(0.0, abs=1e-4)
    gated = tracker.update(
        np.zeros_like(tone), sample_rate_hz=fs, sample_index=tone.size,
        generation=0, signal_kind="unmodulated_carrier",
    )
    assert gated.reason == "below_signal_gate"
    assert gated.held_last_valid is True
    assert gated.correction_hz == pytest.approx(offset, abs=0.01)
    tracker.reset()  # seek/gap releases the held correction and its phase
    assert tracker.reading.reason == "awaiting_samples"
    assert tracker.reading.correction_hz == 0.0
    np.testing.assert_array_equal(tracker.correct(tone, sample_rate_hz=fs), tone)


def test_receiver_inspection_has_channel_scope_and_spectrum_contract():
    inspection = ReceiverInspection(max_samples=10_000)
    fs = 48_000
    audio = np.sin(2 * np.pi * 1000 * np.arange(fs // 10) / fs).astype(np.float32)
    iq = np.exp(2j * np.pi * 1000 * np.arange(5000) / 50_000).astype(np.complex64)
    inspection.append_audio(
        audio, sample_rate_hz=fs, sample_index=0, generation=1, session_id="run"
    )
    inspection.append_channel_iq(
        iq, sample_rate_hz=50_000, sample_index=0, generation=1, session_id="run"
    )
    snapshot = inspection.snapshot(window_s=0.05, points=256)
    assert set(snapshot["streams"]) == {"audio", "iq_envelope", "iq_i", "iq_q"}
    assert snapshot["streams"]["iq_envelope"]["session_id"] == "run"
    spectrum = inspection.snapshot("audio_spectrum")["audio_spectrum"]
    assert set(("frequency_hz", "power_dbfs", "sample_rate_hz")) <= set(spectrum)
    peak = spectrum["frequency_hz"][np.argmax(spectrum["power_dbfs"])]
    assert peak == pytest.approx(1000, abs=25)


def test_channel_iq_nan_is_sanitised_like_audio():
    inspection = ReceiverInspection(max_samples=10_000)
    iq = np.full(1000, 0.5 + 0.25j, dtype=np.complex64)
    iq[10] = complex(np.nan, np.nan)
    inspection.append_channel_iq(
        iq, sample_rate_hz=50_000, sample_index=0, generation=1, session_id="run"
    )
    streams = inspection.snapshot(window_s=0.02, points=256)["streams"]
    for name in ("iq_envelope", "iq_i", "iq_q"):
        assert np.isfinite(np.asarray(streams[name]["values"], dtype=float)).all()
    assert np.isnan(iq[10])  # caller's array not modified
