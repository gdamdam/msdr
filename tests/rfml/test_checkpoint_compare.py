"""Focused checks for the standalone checkpoint comparison utilities."""

import importlib.util
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rfml_checkpoint_compare.py"
SPEC = importlib.util.spec_from_file_location("rfml_checkpoint_compare", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
COMPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPARE)


def test_extract_windows_uses_sample_rate_and_requested_offsets():
    iq = np.arange(50_000 * 3, dtype=np.float32).astype(np.complex64)
    windows = COMPARE.extract_windows(iq, 50_000, [1.0, 2.0])

    assert [window["offset_samples"] for window in windows] == [50_000, 100_000]
    assert all(window["iq"].shape == (4096,) for window in windows)
    assert windows[0]["iq"][0] == 50_000
    assert windows[1]["iq"][0] == 100_000


def test_label_order_verification_follows_upstream_source_chain(tmp_path):
    labels = tmp_path / "labels.py"
    labels.write_text(
        """
from typing import ClassVar, Final
CLASS_FAMILY_DICT: Final[dict[str, str]] = {"first": "a", "second": "b"}
SIGNALS_SHARED_LIST: Final[list[str]] = list(CLASS_FAMILY_DICT.keys())
class TorchSigSignalLists:
    all_signals: ClassVar[list[str]] = SIGNALS_SHARED_LIST
""",
        encoding="utf-8",
    )
    inference = tmp_path / "inference.py"
    inference.write_text("class_list = TorchSigSignalLists.all_signals\n", encoding="utf-8")

    assert COMPARE.read_class_names(labels) == ["first", "second"]
    COMPARE.verify_new_label_order(labels, inference)
