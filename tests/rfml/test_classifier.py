from __future__ import annotations

import contextlib
import hashlib
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.rfml import classifier


def _settings(path="model.ckpt", *, device="cpu", threshold=0.7):
    return SimpleNamespace(model_path=str(path), device=device, score_threshold=threshold)


def test_prepare_iq_normalizes_and_returns_model_input_shape():
    iq = np.full(classifier.SAMPLES + 10, 3 + 4j, dtype=np.complex128)
    prepared = classifier.prepare_iq(iq)

    assert prepared.shape == (1, 2, classifier.SAMPLES)
    assert prepared.dtype == np.float32
    np.testing.assert_allclose(prepared[0, 0], 0.6)
    np.testing.assert_allclose(prepared[0, 1], 0.8)


@pytest.mark.parametrize(
    "iq, message",
    [
        (np.ones(classifier.SAMPLES - 1, dtype=np.complex64), "at least 4096"),
        (np.ones((2, classifier.SAMPLES), dtype=np.complex64), "at least 4096"),
        (np.ones(classifier.SAMPLES, dtype=np.float32), "at least 4096"),
        (np.full(classifier.SAMPLES, np.nan + 0j, dtype=np.complex64), "non-finite"),
        (np.zeros(classifier.SAMPLES, dtype=np.complex64), "silent"),
    ],
)
def test_prepare_iq_rejects_invalid_windows(iq, message):
    with pytest.raises(ValueError, match=message):
        classifier.prepare_iq(iq)


def test_summarize_logits_abstains_as_unknown_below_threshold():
    result = classifier.summarize_logits(
        np.zeros(len(classifier.MANIFEST["classes"])), threshold=0.9
    )

    assert result["status"] == "abstained"
    assert result["classification"] == "UNKNOWN"
    assert result["reason"] == "below score threshold"
    assert len(result["candidates"]) == 3
    assert result["candidates"][0]["label"] == "ook"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("2fsk", "FSK"),
        ("2gmsk", "FSK"),
        ("qpsk", "PSK"),
        ("32qam_cross", "QAM"),
        ("ofdm-1200", "OFDM"),
        ("am-usb", "AM"),
        ("fm", "FM"),
        ("lfm_radar", "CHIRP"),
        ("chirpss", "CHIRP"),
        ("ook", "ASK/PAM"),
        ("16pam", "ASK/PAM"),
        ("unknown-label", "UNKNOWN"),
    ],
)
def test_manifest_label_families(label, expected):
    assert classifier.family(label) == expected


class _TorchDeviceProbe:
    def __init__(self, available):
        self.backends = SimpleNamespace(
            mps=SimpleNamespace(is_available=lambda: available)
        )


@pytest.mark.parametrize(
    ("requested", "mps_available", "expected"),
    [
        ("auto", True, "mps"),
        ("auto", False, "cpu"),
        ("cpu", True, "cpu"),
        ("mps", False, "cpu"),
    ],
)
def test_device_selection(requested, mps_available, expected):
    assert classifier.select_device(_TorchDeviceProbe(mps_available), requested) == expected


def test_device_selection_rejects_unknown_device():
    with pytest.raises(ValueError, match="device must be"):
        classifier.select_device(_TorchDeviceProbe(False), "cuda")


def test_load_rejects_missing_weights_before_importing_torch(tmp_path, monkeypatch):
    def unexpected_import(_name):
        pytest.fail("torch import must follow model file validation")

    monkeypatch.setattr(classifier.importlib, "import_module", unexpected_import)
    with pytest.raises(FileNotFoundError, match="RF model missing"):
        classifier.RFMLClassifier(_settings(tmp_path / "absent.ckpt")).load()


def test_load_rejects_wrong_checkpoint_checksum(tmp_path, monkeypatch):
    path = tmp_path / "bad.ckpt"
    path.write_bytes(b"wrong weights")
    monkeypatch.setitem(classifier.MANIFEST, "size_bytes", path.stat().st_size)
    monkeypatch.setitem(classifier.MANIFEST, "sha256", "0" * 64)
    with pytest.raises(ValueError, match="checksum mismatch"):
        classifier.RFMLClassifier(_settings(path)).load()


class _FakeTensor:
    def __init__(self, value=None, device="cpu"):
        self.value = value
        self.device = device

    def to(self, device):
        return _FakeTensor(self.value, device)

    def cpu(self):
        return self

    def numpy(self):
        return self.value


class _FakeModel:
    def __init__(self, *_args, **_kwargs):
        self.devices = []
        self.calls = 0

    def load_state_dict(self, _state, strict):
        assert strict is True

    def eval(self):
        return self

    def to(self, device):
        self.devices.append(device)
        return self

    def __call__(self, tensor):
        self.calls += 1
        if tensor.device == "mps":
            raise RuntimeError("MPS operator unavailable")
        logits = np.zeros(len(classifier.MANIFEST["classes"]))
        logits[classifier.MANIFEST["classes"].index("fm")] = 4.0
        return _FakeTensor(logits)


class _FakeTorch:
    def __init__(self):
        self.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True))
        self.mps = SimpleNamespace(synchronize=lambda: None)
        self.threads = None

    def set_num_threads(self, count):
        self.threads = count

    def load(self, *_args, **kwargs):
        assert kwargs == {"map_location": "cpu", "weights_only": True}
        return {
            "hyper_parameters": {
                "input_channels": 2,
                "num_classes": 61,
                "xcit_version": "tiny_12_p16_224",
                "ds_method": "downsample",
                "ds_rate": 16,
            },
            "state_dict": {"model.weight": object()},
        }

    @staticmethod
    def inference_mode():
        return contextlib.nullcontext()

    @staticmethod
    def zeros(*_args, device="cpu"):
        return _FakeTensor(device=device)

    @staticmethod
    def from_numpy(value):
        return _FakeTensor(value)


def test_mps_runtime_failure_falls_back_to_cpu_for_load_and_predict(tmp_path, monkeypatch):
    path = tmp_path / "weights.ckpt"
    path.write_bytes(b"pinned test checkpoint")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setitem(classifier.MANIFEST, "size_bytes", path.stat().st_size)
    monkeypatch.setitem(classifier.MANIFEST, "sha256", digest)

    fake_torch = _FakeTorch()
    monkeypatch.setattr(classifier.importlib, "import_module", lambda name: fake_torch)
    fake_xcit = types.ModuleType("msdr.rfml.xcit")
    fake_xcit.XCiT1d = _FakeModel
    monkeypatch.setitem(sys.modules, "msdr.rfml.xcit", fake_xcit)

    model = classifier.RFMLClassifier(_settings(path, device="mps", threshold=0.1))
    model.load()

    assert fake_torch.threads == 2
    assert model.backend == "cpu"
    assert "MPS failed; using CPU" in model.fallback_reason
    assert model.model.devices == ["mps", "cpu"]

    result = model.predict(np.ones(classifier.SAMPLES, dtype=np.complex64), 1_000_000)
    assert result["backend"] == "cpu"
    assert result["classification"] == "FM"
    assert result["status"] == "ok"
    assert result["fallback_reason"] == model.fallback_reason
    assert result["window_s"] == pytest.approx(classifier.SAMPLES / 1_000_000)


class _SequenceModel:
    def __init__(self, labels, strengths=None):
        self.labels = labels
        self.strengths = strengths or [8.0] * len(labels)
        self.calls = 0

    def __call__(self, tensor):
        assert tensor.value.shape == (1, 2, classifier.SAMPLES)
        index = self.calls
        self.calls += 1
        logits = np.zeros(len(classifier.MANIFEST["classes"]))
        logits[classifier.MANIFEST["classes"].index(self.labels[index])] = self.strengths[index]
        return _FakeTensor(logits)


def _sequence_classifier(labels, strengths=None, threshold=0.5):
    instance = classifier.RFMLClassifier(_settings(threshold=threshold))
    instance.model = _SequenceModel(labels, strengths)
    instance.torch = _FakeTorch()
    return instance


def test_predict_requires_unanimous_family_and_retains_window_evidence():
    model = _sequence_classifier(["2fsk", "4fsk", "2fsk"], [8.0, 9.0, 10.0])
    result = model.predict(np.ones(3 * classifier.SAMPLES, dtype=np.complex64), 4096)

    assert result["status"] == "ok"
    assert result["classification"] == "FSK"
    assert result["window_count"] == 3
    assert result["window_s"] == 3
    assert [window["offset_samples"] for window in result["windows"]] == [0, 4096, 8192]
    assert [window["candidates"][0]["label"] for window in result["windows"]] == [
        "2fsk", "4fsk", "2fsk"
    ]
    assert result["score"] == result["windows"][0]["score"]
    assert result["candidates"] == result["windows"][0]["candidates"]
    assert "consensus" in result["preprocessing_version"]


def test_predict_abstains_on_disagreement_or_weak_window():
    iq = np.ones(2 * classifier.SAMPLES, dtype=np.complex64)
    disagreement = _sequence_classifier(["fm", "2fsk"]).predict(iq, 4096)
    assert disagreement["status"] == "abstained"
    assert disagreement["classification"] == "UNKNOWN"
    assert disagreement["reason"] == "window family disagreement"
    assert [window["classification"] for window in disagreement["windows"]] == ["FM", "FSK"]

    weak = _sequence_classifier(["fm", "fm"], [8.0, 1.0]).predict(iq, 4096)
    assert weak["status"] == "abstained"
    assert weak["reason"] == "below score threshold in one or more windows"
    assert weak["windows"][1]["status"] == "abstained"


def test_predict_caps_inference_at_three_windows_and_abstains_on_invalid_window():
    model = _sequence_classifier(["fm", "fm", "fm"])
    iq = np.ones(4 * classifier.SAMPLES, dtype=np.complex64)
    result = model.predict(iq, 4096)
    assert result["window_count"] == 3
    assert model.model.calls == 3

    iq[classifier.SAMPLES : 2 * classifier.SAMPLES] = 0
    invalid = _sequence_classifier(["fm"]).predict(iq, 4096)
    assert invalid["status"] == "abstained"
    assert invalid["classification"] == "UNKNOWN"
    assert invalid["windows"][1]["reason"] == "silent IQ window"
