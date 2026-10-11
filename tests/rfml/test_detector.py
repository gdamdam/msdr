"""Dependency-free tests for the experimental wideband detector adapter."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.rfml import detector
from msdr.rfml.fusion import Candidate


def test_prepare_spectrogram_image_produces_black_hot_bgr_and_coordinates():
    n = 2048
    # A tone gives a bright ridge before inversion.
    iq = np.exp(2j * np.pi * 0.125 * np.arange(n)).astype(np.complex64)
    image, metadata = detector.prepare_spectrogram_image(
        iq, 1_000_000, 2_000_000, n_fft=64, hop_length=32
    )
    assert image.shape == (64, 63, 3)
    assert image.dtype == np.uint8
    assert image.flags.c_contiguous
    assert np.array_equal(image[..., 0], image[..., 1])
    assert np.array_equal(image[..., 1], image[..., 2])
    assert image.min() == 0
    assert image.max() == 255
    # +fs/8 occupies ascending bin 40, hence reversed image row 23.
    assert np.argmin(image[:, 0, 0]) == 23
    box = metadata.box_to_coordinates((1, 2, 3, 4))
    assert box.frequency_start_hz == pytest.approx(
        2_000_000 - 500_000 + 1 * (1_000_000 / 64) - (1_000_000 / 128)
    )
    assert box.sample_start == 64
    assert box.sample_end == 64 + 3 * 32


def test_detector_spectrogram_matches_blackman_power_and_reversed_axes():
    n_fft = 64
    sample = np.arange(n_fft, dtype=np.float32)
    iq = (np.exp(2j * np.pi * 7 * sample / n_fft) +
          0.2 * np.exp(-2j * np.pi * 11 * sample / n_fft)).astype(np.complex64)
    image, metadata = detector.prepare_spectrogram_image(
        iq, 64_000, 1_000_000, n_fft=n_fft, hop_length=32
    )
    assert image.shape == (64, 1, 3)
    assert metadata.frame_count == 1
    # The second tone lies at ascending bin 21 and therefore image row 42.
    assert image[24, 0, 0] < image[42, 0, 0]
    assert image[42, 0, 0] < image[0, 0, 0]


def test_detector_image_matches_upstream_torch_stft_and_cv2_when_installed():
    torch = pytest.importorskip("torch")
    cv2 = pytest.importorskip("cv2")
    rng = np.random.default_rng(79)
    iq = (rng.normal(size=4096) + 1j * rng.normal(size=4096)).astype(np.complex64)
    actual, _ = detector.prepare_spectrogram_image(
        iq, 1_000_000, 2_000_000, n_fft=256, hop_length=128
    )
    # TorchSig v2.1.1 compute_spectrogram uses torchaudio Spectrogram with
    # these torch.stft settings; its YOLO adapter then uses cv2.normalize.
    stft = torch.stft(torch.from_numpy(iq), n_fft=256, hop_length=128,
                      win_length=256, window=torch.blackman_window(256),
                      center=False, onesided=False, return_complex=True)
    power = torch.fft.fftshift(abs(stft) ** 2, dim=0).numpy()
    epsilon = np.max(np.abs(power)) * np.sqrt(1e-20)
    power[power == 0] = epsilon
    db = (10 * np.log10(power))[::-1, :]
    normalized = cv2.normalize(db, np.zeros(db.shape, dtype=np.float32),
                               0, 255, cv2.NORM_MINMAX)
    expected = 255 - cv2.cvtColor(normalized.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    np.testing.assert_array_equal(actual, expected)


def test_tiled_detector_box_inverts_frequency_rows_and_time_columns(monkeypatch, tmp_path):
    adapter = detector.RFMLDetector(
        tmp_path / "unused.pt", n_fft=1024, hop_length=512, tile_bins=512,
        max_tiles=1,
    )
    seen = []

    class FakeArray:
        def __init__(self, values):
            self.values = np.asarray(values)

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.values

    def fake_predict(image):
        seen.append(image.shape)
        return [SimpleNamespace(boxes=SimpleNamespace(
            xyxy=FakeArray([[1, 10, 3, 20]]),
            conf=FakeArray([0.9]), cls=FakeArray([0])), names={0: "signal"})]

    monkeypatch.setattr(adapter, "load", lambda: None)
    monkeypatch.setattr(adapter, "_predict", fake_predict)
    iq = np.exp(2j * np.pi * 0.125 * np.arange(4096)).astype(np.complex64)
    result = adapter.predict(iq, sample_rate_hz=1_024_000,
                             center_frequency_hz=2_000_000)
    assert seen == [(512, 7, 3)]
    candidate = result["candidates"][0]
    # First blind tile covers ascending bins 0..511, image rows 512..1023.
    assert candidate.evidence["tile_start_bin"] == 0
    assert candidate.evidence["box_px"] == [1, 522, 3, 532]
    assert candidate.low_hz == pytest.approx(1_979_500)
    assert candidate.high_hz == pytest.approx(1_989_500)
    assert candidate.start_s == pytest.approx(512 / 1_024_000)
    assert candidate.end_s == pytest.approx(2048 / 1_024_000)


def test_hash_verification_refuses_modified_checkpoint(tmp_path, monkeypatch):
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"checkpoint")
    monkeypatch.setattr(
        detector,
        "MANIFEST",
        {
            **detector.MANIFEST,
            "size_bytes": len(b"checkpoint"),
            "sha256": hashlib.sha256(b"checkpoint").hexdigest(),
        },
    )
    adapter = detector.RFMLDetector(artifact)
    adapter._verify_checkpoint()
    artifact.write_bytes(b"tampered!!")
    with pytest.raises(ValueError, match="size mismatch|checksum mismatch"):
        adapter._verify_checkpoint()


def test_predict_maps_boxes_to_ml_candidates_without_loading_optional_dependencies(
    monkeypatch, tmp_path
):
    artifact = tmp_path / "not-loaded.pt"
    adapter = detector.RFMLDetector(artifact, device="cpu", n_fft=64, hop_length=32)

    class FakeArray:
        def __init__(self, values):
            self.values = np.asarray(values)

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.values

    boxes = SimpleNamespace(
        xyxy=FakeArray([[10.1, 2.0, 20.0, 5.1]]),
        conf=FakeArray([0.83]),
        cls=FakeArray([0]),
    )
    result = SimpleNamespace(boxes=boxes, names={0: "signal"})
    monkeypatch.setattr(adapter, "load", lambda: None)
    monkeypatch.setattr(adapter, "_predict", lambda _image: [result])
    iq = np.exp(2j * np.pi * 0.125 * np.arange(2048)).astype(np.complex64)
    output = adapter.predict(iq, sample_rate_hz=1_000_000, center_frequency_hz=2_000_000)

    assert output["preprocessing_status"] == "experimental_unvalidated"
    assert output["field_validated"] is False
    assert len(output["candidates"]) == 1
    candidate = output["candidates"][0]
    assert isinstance(candidate, Candidate)
    assert candidate.source == "ml"
    assert candidate.score == pytest.approx(0.83)
    assert candidate.evidence["label"] == "signal"
    assert candidate.low_hz < candidate.high_hz
    assert candidate.start_s < candidate.end_s


def test_predict_rejects_capture_over_maximum_before_model_call(monkeypatch, tmp_path):
    adapter = detector.RFMLDetector(tmp_path / "not-loaded.pt")
    monkeypatch.setattr(
        adapter, "_predict", lambda _image: pytest.fail("must reject before inference")
    )
    too_long = np.zeros(detector.MAX_IQ_SAMPLES + 1, dtype=np.complex64)
    with pytest.raises(ValueError, match="no longer than"):
        adapter.predict(too_long, sample_rate_hz=1_000_000, center_frequency_hz=2_000_000)


def test_bad_device_and_threshold_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="device"):
        detector.RFMLDetector(tmp_path / "model.pt", device="cuda")
    with pytest.raises(ValueError, match="score_threshold"):
        detector.RFMLDetector(tmp_path / "model.pt", score_threshold=float("nan"))


@pytest.mark.parametrize("max_tiles", [1, 4])
def test_blind_tile_visits_full_span_regardless_of_focus(tmp_path, max_tiles):
    adapter = detector.RFMLDetector(tmp_path / "unused.pt", max_tiles=max_tiles)
    metadata = SimpleNamespace(
        frequency_bins=2048,
        center_frequency_hz=2_000_000,
        sample_rate_hz=2048,
        bin_width_hz=1,
    )
    expected = [0, 384, 768, 1152, 1536]
    focus = [2_000_000 - 1024 + start + 256 for start in expected]
    selections = [adapter._tile_starts(metadata, focus) for _ in expected]

    assert [tiles[0] for tiles in selections] == expected
    assert all(len(tiles) == max_tiles for tiles in selections)
    assert all(len(set(tiles)) == max_tiles for tiles in selections)


def test_mps_inference_failure_retries_with_cpu_and_resets_predictor(tmp_path):
    adapter = detector.RFMLDetector(tmp_path / "not-loaded.pt", device="mps")
    adapter.backend = "mps"
    adapter.model = SimpleNamespace(predictor=object())
    calls = []

    def predict(**kwargs):
        calls.append(kwargs["device"])
        if kwargs["device"] == "mps":
            raise RuntimeError("MPS operation unavailable")
        return ["cpu-result"]

    adapter.model.predict = predict
    adapter.load = lambda: None
    result = adapter._predict(np.zeros((8, 8, 3), dtype=np.uint8))

    assert result == ["cpu-result"]
    assert calls == ["mps", "cpu"]
    assert adapter.backend == "cpu"
    assert adapter.model.predictor is None
    assert "using CPU" in adapter.fallback_reason


def test_duplicate_tile_boxes_aggregate_with_raw_detection_count(monkeypatch, tmp_path):
    adapter = detector.RFMLDetector(tmp_path / "not-loaded.pt", n_fft=64, hop_length=32)

    class FakeArray:
        def __init__(self, values):
            self.values = np.asarray(values)
        def detach(self): return self
        def cpu(self): return self
        def numpy(self): return self.values

    boxes = SimpleNamespace(
        xyxy=FakeArray([[10, 10, 30, 30], [10.5, 10, 30.5, 30]]),
        conf=FakeArray([0.83, 0.91]), cls=FakeArray([0, 0]),
    )
    monkeypatch.setattr(adapter, "load", lambda: None)
    monkeypatch.setattr(adapter, "_predict", lambda _image: [
        SimpleNamespace(boxes=boxes, names={0: "signal"})
    ])
    iq = np.exp(2j * np.pi * 0.125 * np.arange(2048)).astype(np.complex64)
    output = adapter.predict(iq, sample_rate_hz=1_000_000, center_frequency_hz=2_000_000)

    assert output["raw_candidate_count"] == 2
    assert len(output["candidates"]) == 1
    assert output["candidates"][0].score == pytest.approx(0.91)
    assert output["candidates"][0].evidence["duplicate_count"] == 2


@pytest.mark.parametrize("second", [
    # A neighboring channel with only half of its frequency extent overlapping.
    Candidate("ml", 1_000_050, 1_000_250, 0.0, 0.1, 0.8, {"class_id": 0}),
    # A narrow channel fully enclosed by a broad detection.
    Candidate("ml", 1_000_040, 1_000_060, 0.0, 0.1, 0.8, {"class_id": 0}),
    # Same frequency but a distinct burst separated in time.
    Candidate("ml", 1_000_000, 1_000_100, 0.2, 0.3, 0.8, {"class_id": 0}),
    # Same geometry, different detector class.
    Candidate("ml", 1_000_000, 1_000_100, 0.0, 0.1, 0.8, {"class_id": 1}),
])
def test_duplicate_aggregation_preserves_nearby_channels_bursts_and_classes(second):
    first = Candidate("ml", 1_000_000, 1_000_100, 0.0, 0.1, 0.9, {"class_id": 0})
    assert len(detector._aggregate_duplicate_candidates([first, second])) == 2
