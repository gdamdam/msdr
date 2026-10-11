"""The README's version badge is hand-written; this keeps it equal to
msdr.__version__ so a version bump can't leave it stale."""

import re
from pathlib import Path

import msdr

README = Path(__file__).resolve().parents[1] / "README.md"


def _normalize(version):
    # The badge spells pre-releases the human way ("1.0.0-rc.1") while
    # __version__ uses PEP 440 ("1.0.0rc1"); drop separators around the tag.
    return re.sub(r"[-_.]?(a|b|rc)[-_.]?", r"\1", version)


def test_readme_version_badge_matches_package_version():
    text = README.read_text(encoding="utf-8")
    # shields.io escapes a literal "-" as "--", so the version runs up to the
    # first single "-" (the one before the colour).
    match = re.search(r"badge/version-((?:[^-]|--)+)-", text)
    assert match, "README has no version badge"
    badge = match.group(1).replace("--", "-")
    assert _normalize(badge) == _normalize(msdr.__version__)
