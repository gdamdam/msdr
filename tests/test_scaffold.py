import re
from pathlib import Path

from msdr.ids import uuid7
from msdr.settings import Settings

UUID7_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
FORBIDDEN_IMPORTS = re.compile(r"fastapi|starlette|uvicorn")
CORE_PACKAGES = ("radio", "engine", "library", "journal", "storage")


def test_uuid7_is_time_ordered():
    ids = [uuid7() for _ in range(100)]
    assert all(UUID7_RE.match(i) for i in ids)
    assert ids == sorted(ids)


def test_settings_defaults_and_roundtrip(tmp_path):
    defaults = Settings.load(tmp_path / "none.json")
    assert defaults == Settings()

    path = tmp_path / "settings.json"
    changed = Settings(dwell_seconds=3.5)
    changed.save(path)

    loaded = Settings.load(path)
    assert loaded.dwell_seconds == 3.5


def test_core_packages_do_not_import_web():
    root = Path(__file__).resolve().parent.parent / "msdr"
    for pkg in CORE_PACKAGES:
        for path in (root / pkg).rglob("*.py"):
            text = path.read_text()
            assert not FORBIDDEN_IMPORTS.search(text), f"{path} references a web framework"
