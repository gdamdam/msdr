from __future__ import annotations

import hashlib

import pytest

from msdr.rfml import cli


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size):
        return next(self.chunks, b"")


def _pin(monkeypatch, data: bytes, *, digest: str | None = None, size: int | None = None):
    monkeypatch.setitem(cli.MANIFEST, "url", "https://invalid.example/model")
    monkeypatch.setitem(cli.MANIFEST, "size_bytes", len(data) if size is None else size)
    monkeypatch.setitem(cli.MANIFEST, "sha256", digest or hashlib.sha256(data).hexdigest())


def test_download_writes_verified_pinned_bytes(tmp_path, monkeypatch):
    payload = b"checkpoint bytes"
    _pin(monkeypatch, payload)
    monkeypatch.setattr(
        cli.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse([payload])
    )
    destination = tmp_path / "nested" / "model.ckpt"

    result = cli.download(destination)

    assert destination.read_bytes() == payload
    assert result == {
        "path": str(destination),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "downloaded": True,
    }
    assert list(destination.parent.iterdir()) == [destination]


def test_bad_checksum_cleans_temporary_file_and_leaves_no_destination(tmp_path, monkeypatch):
    payload = b"untrusted checkpoint"
    _pin(monkeypatch, payload, digest="0" * 64)
    monkeypatch.setattr(
        cli.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse([payload])
    )
    destination = tmp_path / "model.ckpt"

    with pytest.raises(ValueError, match="checksum/size mismatch"):
        cli.download(destination)

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_wrong_existing_destination_is_preserved(tmp_path, monkeypatch):
    destination = tmp_path / "model.ckpt"
    original = b"existing unrelated bytes"
    destination.write_bytes(original)
    _pin(monkeypatch, b"supported bytes")
    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("must not overwrite an existing destination"),
    )

    with pytest.raises(ValueError, match="destination exists with different contents"):
        cli.download(destination)

    assert destination.read_bytes() == original


def test_download_rejects_size_overshoot_and_cleans_temporary_file(tmp_path, monkeypatch):
    _pin(monkeypatch, b"tiny", size=3)
    monkeypatch.setattr(
        cli.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse([b"four"])
    )
    destination = tmp_path / "model.ckpt"

    with pytest.raises(ValueError, match="exceeded expected checkpoint size"):
        cli.download(destination)

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "args",
    [
        ["benchmark", "--iterations", "0"],
        ["benchmark", "--iterations", "1001"],
        ["benchmark", "--sample-rate", "0"],
        ["benchmark", "--sample-rate", "nan"],
        ["benchmark", "--score-threshold", "1.1"],
        ["benchmark", "--device", "cuda"],
    ],
)
def test_cli_rejects_invalid_benchmark_arguments(args, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)

    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_reports_missing_model_without_loading_torch(tmp_path, capsys):
    model_path = tmp_path / "missing.ckpt"

    with pytest.raises(SystemExit) as exc:
        cli.main(["benchmark", "--model-path", str(model_path)])

    assert exc.value.code == 1
    assert "RF ML unavailable: RF model missing" in capsys.readouterr().err
    assert not model_path.exists()
