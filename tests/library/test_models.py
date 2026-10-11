import pytest
from pydantic import ValidationError

from msdr.library.models import CATEGORY_TAGS, Signal, Tag


def test_signal_color_accepts_valid_and_rejects_bad():
    signal = Signal(name="X", frequency_hz=100_000_000, color="red")
    assert signal.color == "red"
    with pytest.raises(ValidationError):
        Signal(name="X", frequency_hz=100_000_000, color="not-a-color")


def test_signal_coordinates_must_be_a_valid_pair():
    from pydantic import ValidationError

    assert Signal(name="X", frequency_hz=1, latitude=0, longitude=0).latitude == 0
    with pytest.raises(ValidationError):
        Signal(name="X", frequency_hz=1, latitude=91, longitude=0)
    with pytest.raises(ValidationError):
        Signal(name="X", frequency_hz=1, latitude=20)


def test_tag_color_accepts_valid_and_rejects_bad():
    tag = Tag(name="custom", color="violet")
    assert tag.color == "violet"
    with pytest.raises(ValidationError):
        Tag(name="custom", color="not-a-color")


def test_category_tags_has_eleven_entries_with_colors():
    assert len(CATEGORY_TAGS) == 11
    names = [name for name, _ in CATEGORY_TAGS]
    assert len(names) == len(set(names))  # no duplicate categories
    for _, color in CATEGORY_TAGS:
        assert isinstance(color, str) and color
