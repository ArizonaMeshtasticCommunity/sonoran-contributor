"""Small bits of geometry: rounding the station, and distances."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0
# Metres in one degree of latitude, near enough for rounding.
METRES_PER_DEGREE = 111_320.0


def round_position(lat: float, lon: float, precision_m: int) -> tuple[float, float]:
    """Snap a position to a grid about `precision_m` metres on a side.

    The station's position is rounded before it leaves the site (design
    section 8). The grid is fixed, so the same antenna always rounds to
    the same point, and a run of heartbeats cannot be averaged back to
    the true one. Zero keeps the position, to five decimals.
    """
    if precision_m <= 0:
        return round(lat, 5), round(lon, 5)
    lat_step = precision_m / METRES_PER_DEGREE
    snapped_lat = max(-90.0, min(90.0, round(lat / lat_step) * lat_step))
    # The east-west step widens towards the poles, to stay near
    # `precision_m` metres. It uses the snapped latitude, so every antenna
    # in one row of the grid uses the same step.
    cos_lat = max(math.cos(math.radians(snapped_lat)), 0.01)
    lon_step = min(precision_m / (METRES_PER_DEGREE * cos_lat), 360.0)
    snapped_lon = round(lon / lon_step) * lon_step
    if snapped_lon > 180:
        snapped_lon -= 360
    elif snapped_lon < -180:
        snapped_lon += 360
    return round(snapped_lat, 5), round(snapped_lon, 5)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
