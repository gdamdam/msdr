from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.rfml.corpus import ExampleWriter, comparison, evaluate


def _settings(path, **overrides):
    values = {
        "export_dir": str(path),
        "export_max_bytes": 2_000_000,
        "export_mode": "disagreements",
        "export_interval_s": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _iq(value=0.25):
    return np.full(4096, value + 0.0j, dtype=np.complex64)


def _metadata(kind="fm_fsk"):
    return {"frequency_hz": 145_000_000.0, "dsp": {"modulation": {"kind": kind}}}


def test_disabled_export_does_not_create_a_directory(tmp_path):
    target = tmp_path / "disabled"
    writer = ExampleWriter(_settings(""))

    assert writer.save(_iq(), 48_000, _metadata(), {"status": "ok"}) is None
    assert not target.exists()
    assert writer.saved == 0


def test_export_writes_iq_metadata_and_physical_spectrogram_without_pickle(tmp_path):
    writer = ExampleWriter(_settings(tmp_path))
    prediction = {"status": "ok", "classification": "PSK", "score": 0.9}

    saved_path = writer.save(_iq(), 48_000, _metadata(), prediction)

    assert saved_path is not None
    with np.load(saved_path, allow_pickle=False) as artifact:
        assert set(artifact.files) == {"iq", "metadata", "spectrogram"}
        np.testing.assert_array_equal(artifact["iq"], _iq())
        assert artifact["iq"].dtype == np.complex64
        assert artifact["spectrogram"].ndim == 2
        payload = json.loads(str(artifact["metadata"].item()))
    assert payload["comparison"] == "disagreement"
    assert payload["ground_truth"] is None
    assert payload["input"] == _metadata()
    assert payload["prediction"] == prediction
    assert payload["spectrogram"]["sample_rate_hz"] == 48_000
    assert payload["spectrogram"]["center_frequency_hz"] == 145_000_000
    assert writer.saved == 1


def test_export_preserves_three_windows_and_rejects_longer_input(tmp_path):
    writer = ExampleWriter(_settings(tmp_path))
    iq = np.concatenate([_iq(1), _iq(2), _iq(3)])
    prediction = {"status": "abstained", "classification": "UNKNOWN"}
    saved = writer.save(iq, 48_000, _metadata(), prediction)
    with np.load(saved, allow_pickle=False) as artifact:
        np.testing.assert_array_equal(artifact["iq"], iq)
    with pytest.raises(ValueError, match="4096"):
        writer.save(np.ones(3 * 4096 + 1, dtype=np.complex64), 48_000,
                    _metadata(), prediction)


def test_disagreement_mode_keeps_abstentions_and_skips_compatible_predictions(tmp_path):
    writer = ExampleWriter(_settings(tmp_path))
    compatible = {"status": "ok", "classification": "FM"}
    abstained = {"status": "abstained", "classification": "UNKNOWN"}

    assert comparison(_metadata()["dsp"], compatible) == "compatible"
    assert comparison(_metadata()["dsp"], abstained) == "ml_abstained"
    assert writer.save(_iq(), 48_000, _metadata(), compatible) is None
    assert list(tmp_path.glob("*.npz")) == []
    saved = writer.save(_iq(), 48_000, _metadata(), abstained)
    assert saved is not None
    with np.load(saved, allow_pickle=False) as artifact:
        assert json.loads(str(artifact["metadata"].item()))["comparison"] == "ml_abstained"


def test_existing_files_count_toward_quota_and_are_preserved(tmp_path):
    prior = tmp_path / "operator-example.npz"
    prior.write_bytes(b"preserve this")
    writer = ExampleWriter(_settings(tmp_path, export_max_bytes=len(prior.read_bytes())))

    assert writer.save(
        _iq(), 48_000, _metadata(), {"status": "ok", "classification": "PSK"}
    ) is None
    assert prior.read_bytes() == b"preserve this"
    assert list(tmp_path.iterdir()) == [prior]
    assert writer.saved == 0


def test_rate_limit_allows_one_example_per_interval(tmp_path):
    writer = ExampleWriter(_settings(tmp_path, export_interval_s=60.0))
    prediction = {"status": "ok", "classification": "PSK"}

    assert writer.save(_iq(), 48_000, _metadata(), prediction) is not None
    assert writer.save(_iq(), 48_000, _metadata(), prediction) is None
    assert len(list(tmp_path.glob("*.npz"))) == 1
    assert writer.saved == 1


class _FixtureClassifier:
    def predict(self, iq, sample_rate_hz):
        marker = int(iq[0].real)
        if marker == 1:
            return {"status": "ok", "classification": "FM"}
        if marker == 2:
            return {"status": "abstained", "classification": "UNKNOWN"}
        if marker == 3:
            return {"status": "ok", "classification": "FSK"}
        raise RuntimeError("fixture inference failure")


def _evaluation_manifest(tmp_path):
    rows = []
    for name, marker, label in [
        ("good", 1, "FM"),
        ("abstain", 2, "FM"),
        ("noise_false_accept", 3, "NOISE"),
        ("error", 4, "PSK"),
    ]:
        path = tmp_path / f"{name}.npy"
        np.save(path, _iq(marker), allow_pickle=False)
        rows.append({"path": path.name, "sample_rate_hz": 48_000, "label": label})
    manifest = tmp_path / "labels.jsonl"
    manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return manifest


def test_evaluation_reports_manual_label_counts_denominators_and_errors(tmp_path):
    report = evaluate(_evaluation_manifest(tmp_path), _FixtureClassifier())

    assert report["examples"] == 4
    assert report["provenance_complete"] is False
    assert report["counts"] == {
        "known_examples": 3,
        "accepted": 2,
        "known_accepted": 1,
        "correct": 1,
        "rejection_examples": 1,
        "false_accepts": 1,
        "rejection_errors": 0,
        "errors": 1,
    }
    assert report["coverage"] == pytest.approx(2 / 3)
    assert report["known_accuracy_including_abstentions"] == pytest.approx(1 / 3)
    assert report["selective_accuracy"] == 1.0
    assert report["rejection_false_accept_rate"] == 1.0
    assert report["confusion"]["FM"] == {"FM": 1, "UNKNOWN": 1}
    assert report["confusion"]["NOISE"] == {"FSK": 1}
    assert report["confusion"]["PSK"] == {"ERROR": 1}
    assert "representativeness" in report["limitations"]


def test_evaluation_reports_held_out_session_provenance(tmp_path):
    manifest = _evaluation_manifest(tmp_path)
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    for index, row in enumerate(rows):
        row.update(session_id=f"session-{index % 2}", split="test")
    manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    report = evaluate(manifest, _FixtureClassifier())
    assert report["provenance_complete"] is True
    assert report["evaluation_splits"] == ["test"]
    assert report["evaluation_sessions"] == ["session-0", "session-1"]
    assert report["results"][0]["session_id"] == "session-0"


def test_evaluation_emits_explicit_zero_rejection_counts(tmp_path):
    path = tmp_path / "quiet.npy"
    np.save(path, _iq(2), allow_pickle=False)
    manifest = tmp_path / "quiet.jsonl"
    manifest.write_text(json.dumps({"path": path.name, "sample_rate_hz": 48_000,
                                    "label": "NOISE"}) + "\n")
    report = evaluate(manifest, _FixtureClassifier())
    assert report["counts"]["false_accepts"] == 0
    assert report["counts"]["rejection_errors"] == 0
    assert report["rejection_false_accept_rate"] == 0.0


def test_evaluation_excludes_rejection_errors_and_nulls_unevaluated_rates(tmp_path):
    path = tmp_path / "error.npy"
    np.save(path, _iq(4), allow_pickle=False)
    manifest = tmp_path / "labels.jsonl"
    manifest.write_text(
        json.dumps({"path": path.name, "label": "NOISE", "sample_rate_hz": 48_000}) + "\n"
    )
    report = evaluate(manifest, _FixtureClassifier())
    assert report["counts"]["rejection_errors"] == 1
    assert report["coverage"] is None
    assert report["rejection_false_accept_rate"] is None

    accepted_path = tmp_path / "accepted.npy"
    np.save(accepted_path, _iq(3), allow_pickle=False)
    manifest.write_text(
        manifest.read_text()
        + json.dumps({"path": accepted_path.name, "label": "NOISE", "sample_rate_hz": 48_000})
        + "\n"
    )
    report = evaluate(manifest, _FixtureClassifier())
    assert report["rejection_false_accept_rate"] == 1.0
    assert report["coverage"] == 1.0


@pytest.mark.parametrize(
    "row",
    [
        {"label": "unverified", "sample_rate_hz": 48_000},
        {"label": "FM", "sample_rate_hz": 0},
        {"label": "FM", "sample_rate_hz": float("nan")},
    ],
)
def test_evaluation_rejects_invalid_truth_or_sample_rate(tmp_path, row):
    manifest = tmp_path / "bad.jsonl"
    manifest.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        evaluate(manifest, _FixtureClassifier())


def test_evaluation_rejects_invalid_iq_sample_file(tmp_path):
    sample_path = tmp_path / "bad.npy"
    np.save(sample_path, np.ones(100, dtype=np.complex64), allow_pickle=False)
    manifest = tmp_path / "labels.jsonl"
    manifest.write_text(
        json.dumps({"path": sample_path.name, "sample_rate_hz": 48_000, "label": "FM"})
        + "\n"
    )

    with pytest.raises(ValueError, match="invalid evaluation IQ"):
        evaluate(manifest, _FixtureClassifier())


def test_evaluation_caps_classifier_input_at_three_windows(tmp_path):
    sample_path = tmp_path / "long.npy"
    np.save(sample_path, np.ones(4 * 4096, dtype=np.complex64), allow_pickle=False)
    manifest = tmp_path / "labels.jsonl"
    manifest.write_text(
        json.dumps({"path": sample_path.name, "sample_rate_hz": 48_000, "label": "FM"})
        + "\n"
    )

    class CheckingClassifier:
        def predict(self, iq, _rate):
            assert len(iq) == 3 * 4096
            return {"status": "ok", "classification": "FM"}

    report = evaluate(manifest, CheckingClassifier())
    assert report["counts"]["correct"] == 1
