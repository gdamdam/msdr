import json

import numpy as np
import pytest

from msdr.rfml.validation_capture import save_capture


def test_capture_preserves_operator_label_separate_from_truth(tmp_path):
    iq = np.exp(1j * np.arange(8192)).astype(np.complex64)
    result = save_capture(tmp_path, iq, 50000, {"frequency_hz": 144390000},
                          "digital", "APRS not yet decoded")
    with np.load(result["path"], allow_pickle=False) as archive:
        np.testing.assert_array_equal(archive["iq"], iq)
        metadata = json.loads(str(archive["metadata"]))
        assert metadata["ground_truth"] is None
        assert metadata["operator_annotation"]["label"] == "digital"
        assert metadata["duration_s"] == 8192 / 50000
        assert archive["spectrogram"].ndim == 2


def test_capture_rejects_bad_iq_and_quota(tmp_path, monkeypatch):
    from msdr.rfml import validation_capture
    with pytest.raises(ValueError):
        save_capture(tmp_path, np.ones(12), 50000, {}, "voice", "")
    monkeypatch.setattr(validation_capture, "_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="quota"):
        save_capture(tmp_path, np.ones(4096, dtype=complex), 50000,
                     {"frequency_hz": 144390000}, "uncertain", "")
    assert not list(tmp_path.glob("*.npz"))


def test_long_capture_retains_all_iq_with_bounded_preview(tmp_path):
    iq = np.ones(1_500_000, dtype=np.complex64)
    result = save_capture(tmp_path, iq, 300000, {"frequency_hz": 160815000}, "voice", "")
    assert result["duration_s"] == 5
    with np.load(result["path"], allow_pickle=False) as archive:
        assert archive["iq"].size == iq.size
        assert archive["spectrogram"].shape[0] <= 1024
