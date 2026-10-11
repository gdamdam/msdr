import json

from msdr.allocations.importer import main
from msdr.allocations.store import AllocationStore


def test_check_bundled_dataset(capsys):
    assert main(["--check", str(AllocationStore.bundled_path())]) == 0
    out = capsys.readouterr().out
    assert "ok" in out and "ntia_table" in out and "ecfr_2_106" in out


def test_check_rejects_corrupt_file(tmp_path, capsys):
    bad = tmp_path / "ds.json"
    bad.write_text("{not json")
    assert main(["--check", str(bad)]) == 1
    assert str(bad) in capsys.readouterr().err


TINY = {
    "bands": [
        {
            "lo_hz": 0,
            "hi_hz": 100,
            "column": "itu_r1",
            "source_id": "itu_rr",
            "services": [{"name": "FIXED", "priority": "primary", "raw": "FIXED 5.1"}],
            "footnotes": ["5.1"],
        },
        {
            "lo_hz": 100,
            "hi_hz": 200,
            "column": "itu_r1",
            "source_id": "itu_rr",
            "services": [],
            "unallocated": True,
        },
    ],
    "footnotes": [{"id": "5.1", "text": "tiny", "source_id": "itu_rr", "kind": "international"}],
    "sources": [{"id": "itu_rr", "title": "ITU RR (user copy)", "publisher": "ITU", "url": "file"}],
}


def test_from_file_accepts_valid_normalized_file(tmp_path, capsys):
    src = tmp_path / "tiny.json"
    src.write_text(json.dumps(TINY))
    out = tmp_path / "out" / "dataset.json"
    assert main(["--from-file", str(src), "--out", str(out)]) == 0
    ds = json.loads(out.read_text())
    assert ds["schema_version"] == 1 and len(ds["bands"]) == 2
    s = ds["sources"][0]
    assert (s["id"], s["lo_hz"], s["hi_hz"], s["band_count"], s["origin"]) == (
        "itu_rr",
        0,
        200,
        2,
        "imported",
    )
    assert s["provenance"]["fetched_url"] == str(src) and len(s["provenance"]["sha256"]) == 64
    assert "itu_r1=2" in capsys.readouterr().out
    # The written file passes --check, and a rewrite keeps the previous copy.
    assert main(["--check", str(out)]) == 0
    assert main(["--from-file", str(src), "--out", str(out)]) == 0
    assert (out.parent / "dataset.prev.json").exists()


def test_from_file_resolves_footnotes_against_bundled_union(tmp_path):
    partial = json.loads(json.dumps(TINY))
    partial["bands"][0]["footnotes"] = ["5.53"]  # defined only in the bundled dataset
    partial["footnotes"] = []
    src = tmp_path / "partial.json"
    src.write_text(json.dumps(partial))
    out = tmp_path / "out.json"
    assert main(["--from-file", str(src), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["footnotes"] == []


def test_from_file_rejects_invalid_file(tmp_path, capsys):
    bad = json.loads(json.dumps(TINY))
    bad["bands"][1]["lo_hz"] = 150  # gap 100-150
    bad["bands"][0]["footnotes"] = ["5.999"]  # unresolved
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(bad))
    out = tmp_path / "out.json"
    assert main(["--from-file", str(src), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "gap 100 Hz-150 Hz" in err and "footnote 5.999 unresolved" in err
    assert not out.exists()


def test_from_file_rejects_json_list_as_import_failure(tmp_path, capsys):
    src = tmp_path / "list.json"
    src.write_text("[]")
    assert main(["--from-file", str(src), "--out", str(tmp_path / "out.json")]) == 1
    assert "not a JSON object" in capsys.readouterr().err


def test_fetch_bytes_wraps_corrupt_gzip_and_tolerates_unknown_content_range(monkeypatch):
    import gzip
    import io

    import pytest

    from msdr.allocations import importer

    class _Resp(io.BytesIO):
        def __init__(self, body, headers, status=200):
            super().__init__(body)
            self.headers, self.status = headers, status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    good = gzip.compress(b"hello")
    monkeypatch.setattr(
        importer.urllib.request,
        "urlopen",
        lambda req, timeout: _Resp(
            good, {"Content-Encoding": "gzip", "Content-Range": "bytes 0-9/*"}, 206
        ),
    )
    assert importer.fetch_bytes("http://x", accept_gzip=True, retries=1).data == b"hello"

    for body in (good[:-6], b"\x1f\x8b" + b"garbage" * 4):  # truncated / corrupt
        monkeypatch.setattr(
            importer.urllib.request,
            "urlopen",
            lambda req, timeout, body=body: _Resp(body, {"Content-Encoding": "gzip"}),
        )
        with pytest.raises(importer.AllocationImportError):
            importer.fetch_bytes("http://x", accept_gzip=True, retries=1)


def test_write_atomic_keeps_dataset_present_and_saves_previous(tmp_path, monkeypatch):
    from msdr.allocations import importer

    target = tmp_path / "dataset.json"
    importer.write_atomic(target, {"v": 1})
    seen = []
    real_replace = importer.os.replace
    monkeypatch.setattr(
        importer.os, "replace", lambda a, b: (seen.append(target.exists()), real_replace(a, b))[1]
    )
    importer.write_atomic(target, {"v": 2})
    assert all(seen)  # dataset.json never absent during the swap
    assert json.loads(target.read_text()) == {"v": 2}
    assert json.loads((tmp_path / "dataset.prev.json").read_text()) == {"v": 1}
