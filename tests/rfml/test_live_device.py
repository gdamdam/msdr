"""Native Metal aborts must not bring down the capture server."""
from types import SimpleNamespace

import pytest

from msdr.rfml import live_device
from msdr.rfml.detector_runtime import RFMLDetectorWorker
from msdr.rfml.runtime import RFMLWorker


@pytest.mark.parametrize("requested", ["auto", "mps", "cpu"])
@pytest.mark.parametrize("worker_type", [RFMLWorker, RFMLDetectorWorker])
def test_macos_live_models_load_on_cpu_without_changing_preferences(
    monkeypatch, requested, worker_type
):
    monkeypatch.setattr(live_device.sys, "platform", "darwin")
    settings = SimpleNamespace(enabled=True, device=requested)
    loaded = []

    def factory(effective):
        assert effective.device == "cpu"
        return SimpleNamespace(load=lambda: loaded.append(True))

    worker = worker_type(settings, lambda *_: None, factory)
    if worker_type is RFMLWorker:
        worker._load_classifier()
    else:
        worker._load()
    assert loaded == [True]
    assert settings.device == requested
    assert bool(worker.status()["device_note"]) == (requested != "cpu")


def test_other_platforms_keep_device_preference(monkeypatch):
    monkeypatch.setattr(live_device.sys, "platform", "linux")
    settings = SimpleNamespace(device="auto")
    assert live_device.live_settings(settings) == (settings, None)
