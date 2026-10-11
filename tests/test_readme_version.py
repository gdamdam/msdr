"""The README's version badge is hand-written; this keeps it equal to
msdr.__version__ so a version bump can't leave it stale."""

import re
from pathlib import Path

import msdr

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_version_badge_matches_package_version():
    text = README.read_text(encoding="utf-8")
    match = re.search(r"badge/version-([0-9.]+)-", text)
    assert match, "README has no version badge"
    assert match.group(1) == msdr.__version__
