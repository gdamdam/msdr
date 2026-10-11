"""msdr/rules/geo.py: haversine distance and point-in-polygon."""

from __future__ import annotations

import pytest

from msdr.rules.geo import haversine_m, point_in_circle, point_in_polygon

# A small square around the origin, roughly 111 m per 0.001 degree of
# latitude at the equator.
SQUARE = [(-0.001, -0.001), (-0.001, 0.001), (0.001, 0.001), (0.001, -0.001)]


def test_haversine_m_same_point_is_zero():
    assert haversine_m(10.0, 20.0, 10.0, 20.0) == 0.0


def test_haversine_m_one_degree_latitude_is_about_111_km():
    d = haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < d < 112_000


def test_point_in_circle():
    assert point_in_circle(0.0001, 0.0, 0.0, 0.0, 50.0)
    assert not point_in_circle(1.0, 1.0, 0.0, 0.0, 50.0)


def test_point_in_polygon_inside_and_outside():
    assert point_in_polygon(0.0, 0.0, SQUARE)
    assert not point_in_polygon(1.0, 1.0, SQUARE)


def test_point_in_polygon_requires_at_least_3_points():
    with pytest.raises(ValueError, match="3 points"):
        point_in_polygon(0.0, 0.0, [(0.0, 0.0), (1.0, 1.0)])
