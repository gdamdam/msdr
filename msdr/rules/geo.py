"""Plain-geometry helpers for the drone geofence rule kind (docs/rules.md).

No projection library: a geofence is a small local area (an operator-drawn
circle or polygon, typically tens to a few thousand metres across), so an
equirectangular approximation -- treating degrees of longitude as scaled by
cos(latitude) -- is accurate enough, and polygon containment is done directly
in lat/lon with that same scaling baked into the cross-product test.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def point_in_polygon(lat: float, lon: float, points: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test. `points` is `[(lat, lon), ...]`, at
    least 3 vertices, implicitly closed (the last point connects back to the
    first). On the boundary itself the result is whichever side the
    even-odd rule lands on -- never asserted as a hard guarantee."""
    if len(points) < 3:
        raise ValueError("a polygon needs at least 3 points")
    inside = False
    n = len(points)
    for i in range(n):
        lat1, lon1 = points[i]
        lat2, lon2 = points[(i + 1) % n]
        crosses = (lon1 > lon) != (lon2 > lon)
        if crosses:
            lon_span = lon2 - lon1
            lat_at_lon = lat1 if lon_span == 0 else lat1 + (lat2 - lat1) * (lon - lon1) / lon_span
            if lat < lat_at_lon:
                inside = not inside
    return inside


def point_in_circle(
    lat: float, lon: float, center_lat: float, center_lon: float, radius_m: float
) -> bool:
    return haversine_m(lat, lon, center_lat, center_lon) <= radius_m
