from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from msdr.rfml import cli
from msdr.rfml.fusion import Candidate


def _read_output(capsys):
    return json.loads(capsys.readouterr().out)


def test_evaluate_command_loads_classifier_and_prints_corpus_report(
    tmp_path, monkeypatch, capsys
):
    iq_path = tmp_path / "tone.npy"
    np.save(iq_path, np.ones(4096, dtype=np.complex64), allow_pickle=False)
    manifest = tmp_path / "labels.jsonl"
    manifest.write_text(
        json.dumps({"path": iq_path.name, "sample_rate_hz": 48_000, "label": "FM"})
        + "\n"
    )

    class FakeClassifier:
        def __init__(self, settings):
            self.settings = settings
            self.loaded = False

        def load(self):
            self.loaded = True

        def predict(self, iq, sample_rate_hz):
            assert self.loaded
            assert iq.shape == (4096,)
            assert sample_rate_hz == 48_000
            return {"status": "ok", "classification": "FM"}

    monkeypatch.setattr(cli, "RFMLClassifier", FakeClassifier)
    cli.main(["evaluate", str(manifest), "--model-path", "fake-model", "--device", "cpu"])

    report = _read_output(capsys)
    assert report["examples"] == 1
    assert report["counts"]["correct"] == 1
    assert report["known_accuracy_including_abstentions"] == 1.0


def test_detect_command_serializes_candidates_metadata_and_dsp_fusion(
    tmp_path, monkeypatch, capsys
):
    iq_path = tmp_path / "capture.npy"
    np.save(iq_path, np.ones(512, dtype=np.complex64), allow_pickle=False)
    dsp_path = tmp_path / "dsp.json"
    dsp_path.write_text(
        json.dumps(
            [
                {
                    "source": "dsp",
                    "low_hz": 99_900_000.0,
                    "high_hz": 100_100_000.0,
                    "start_s": 0.0,
                    "end_s": 0.05,
                    "score": 0.8,
                    "evidence": {"detector": "fixture"},
                }
            ]
        )
    )
    ml_candidate = Candidate(
        "ml", 99_950_000.0, 100_050_000.0, 0.0, 0.05, 0.72, {"label": "fixture"}
    )

    class FakeDetector:
        def __init__(self, model_path, *, device, score_threshold):
            assert model_path == "fixture.pt"
            assert device == "cpu"
            assert score_threshold == 0.4

        def predict(self, iq, *, sample_rate_hz, center_frequency_hz):
            assert iq.shape == (512,)
            assert sample_rate_hz == 1_000_000
            assert center_frequency_hz == 100_000_000
            return {
                "status": "ok",
                "model_id": "fixture-detector",
                "spectrogram": {"n_fft": 256, "frame_count": 3},
                "candidates": [ml_candidate],
            }

    import msdr.rfml.detector as detector_module

    monkeypatch.setattr(detector_module, "RFMLDetector", FakeDetector)
    cli.main(
        [
            "detect",
            "--iq",
            str(iq_path),
            "--sample-rate",
            "1000000",
            "--center-frequency",
            "100000000",
            "--model-path",
            "fixture.pt",
            "--device",
            "cpu",
            "--score-threshold",
            "0.4",
            "--dsp-json",
            str(dsp_path),
        ]
    )

    output = _read_output(capsys)
    assert output["model_id"] == "fixture-detector"
    assert output["spectrogram"] == {"n_fft": 256, "frame_count": 3}
    assert output["elapsed_s_including_load"] >= 0
    assert output["candidates"] == [
        {
            "source": "ml",
            "low_hz": 99_950_000.0,
            "high_hz": 100_050_000.0,
            "start_s": 0.0,
            "end_s": 0.05,
            "score": 0.72,
            "evidence": {"label": "fixture"},
        }
    ]
    assert len(output["fusion"]) == 1
    assert output["fusion"][0]["dsp"]["source"] == "dsp"
    assert output["fusion"][0]["ml"]["source"] == "ml"


def test_detect_rejects_candidates_marked_as_non_dsp_source(
    tmp_path, monkeypatch, capsys
):
    iq_path = tmp_path / "capture.npy"
    np.save(iq_path, np.ones(512, dtype=np.complex64), allow_pickle=False)
    dsp_path = tmp_path / "bad-dsp.json"
    dsp_path.write_text(
        json.dumps(
            [
                {
                    "source": "ml",
                    "low_hz": 99_900_000.0,
                    "high_hz": 100_100_000.0,
                    "start_s": 0.0,
                    "end_s": 0.05,
                    "score": 0.8,
                }
            ]
        )
    )

    class FakeDetector:
        def __init__(self, *_args, **_kwargs):
            pass

        def predict(self, *_args, **_kwargs):
            return {"candidates": [], "status": "ok"}

    import msdr.rfml.detector as detector_module

    monkeypatch.setattr(detector_module, "RFMLDetector", FakeDetector)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "detect",
                "--iq",
                str(iq_path),
                "--sample-rate",
                "1000000",
                "--center-frequency",
                "100000000",
                "--dsp-json",
                str(dsp_path),
            ]
        )
    assert exc.value.code == 1
    assert "--dsp-json must contain only DSP candidates" in capsys.readouterr().err


def test_download_detector_uses_pinned_manifest_and_prints_result(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def fake_download(path, manifest):
        captured["path"] = path
        captured["manifest"] = manifest
        return {"path": str(path), "sha256": manifest["sha256"], "downloaded": True}

    monkeypatch.setattr(cli, "download", fake_download)
    destination = tmp_path / "wideband.pt"
    cli.main(["download-detector", "--model-path", str(destination)])

    expected = json.loads(Path(cli.__file__).with_name("detector.json").read_text())
    assert captured["path"] == destination
    assert captured["manifest"] == expected
    output = _read_output(capsys)
    assert output["path"] == str(destination)
    assert output["sha256"] == expected["sha256"]
    assert output["downloaded"] is True
