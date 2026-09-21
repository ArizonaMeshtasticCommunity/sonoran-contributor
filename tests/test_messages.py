"""The messages, the station rounding, and the contract they must pass."""

import math

import pytest

from rf_agent.geo import distance_m, round_position
from rf_agent.messages import Counts, batch, observation, status, timestamp

from conftest import ROOT, assert_valid, contract_errors, validate

T = 1_790_100_000.0


def test_examples_still_pass():
    assert validate.main(sorted(str(p) for p in (ROOT / "examples").glob("*.json"))) == 0


def test_the_date_time_format_is_checked():
    # Without rfc3339-validator installed, jsonschema skips date-time, and
    # every other test here would pass a bad time.
    message = batch("test-site", "adsb", [], T)
    message["sent"] = "yesterday"
    assert contract_errors(message)


def test_observation_cleans_what_it_can():
    p = observation(
        "adsb",
        " A1B2C3 ",
        T,
        33.123456789,
        -112.0,
        alt_m=70_000,
        alt_ref="baro",
        gs_kn=-3,
        track_deg=360.0,
        rssi_db=float("nan"),
        meta={"callsign": "", "squawk": None, "category": "A3"},
    )
    assert p.id == "a1b2c3"
    assert p.lat == 33.123457
    assert p.alt_m is None and p.alt_ref is None
    assert p.gs_kn is None and p.rssi_db is None
    assert p.track_deg == 0.0
    assert p.meta == {"category": "A3"}
    assert p.to_json() == {
        "id": "a1b2c3",
        "t": "2026-09-22T18:00:00.000Z",
        "lat": 33.123457,
        "lon": -112.0,
        "track_deg": 0.0,
        "meta": {"category": "A3"},
    }


@pytest.mark.parametrize(
    "ident,lat,lon",
    [
        ("a1b2c", 1, 1),
        ("n12345", 1, 1),
        ("~~a1b2c3", 1, 1),
        ("a1b2c3", 90.5, 1),
        ("a1b2c3", 1, math.inf),
        ("a1b2c3", None, 1),
        ("a1b2c3", True, 1),
    ],
)
def test_observation_refuses_what_is_not_a_point(ident, lat, lon):
    assert observation("adsb", ident, T, lat, lon) is None


def test_big_meta_is_dropped_not_the_point():
    p = observation("adsb", "a1b2c3", T, 1, 1, meta={"callsign": "x" * 2000})
    assert p is not None and p.meta == {}


def test_timestamp():
    assert timestamp(T + 0.4567) == "2026-09-22T18:00:00.456Z"


def test_batch_limit():
    p = observation("adsb", "a1b2c3", T, 1, 1)
    assert_valid(batch("test-site", "adsb", [p] * 100, T))
    with pytest.raises(ValueError):
        batch("test-site", "adsb", [p] * 101, T)


def test_status_with_and_without_a_position():
    counts = Counts(heard=10, sent=2, dropped=8)
    with_pos = status(
        "test-site",
        T,
        kinds=["adsb", "uat"],
        counts=counts,
        agent_version="0.1.0",
        position=(32.8, -113.3),
        precision_m=1000,
    )
    assert_valid(with_pos)
    assert with_pos["counts"] == {"heard": 10, "sent": 2, "dropped": 8}
    without = status(
        "test-site", T, kinds=["adsb"], counts=Counts(), agent_version="0.1.0",
        position=None, precision_m=1000,
    )  # fmt: skip
    assert_valid(without)
    assert "lat" not in without and "precision_m" not in without


def test_counts_take_and_give_back():
    counts = Counts(5, 3, 2)
    taken = counts.take()
    assert (counts.heard, counts.sent, counts.dropped) == (0, 0, 0)
    counts.heard += 1
    counts.give_back(taken)
    assert (counts.heard, counts.sent, counts.dropped) == (6, 3, 2)


@pytest.mark.parametrize("precision", [100, 1000, 5000, 25_000])
def test_rounding_stays_within_the_precision(precision):
    for lat, lon in [(33.4212, -111.9387), (32.80013, -113.2999), (-33.87, 151.21), (64.8, -147.7)]:
        rlat, rlon = round_position(lat, lon, precision)
        # At most half the diagonal of a grid cell away.
        assert distance_m(lat, lon, rlat, rlon) <= precision * 0.75
        if precision >= 1000:
            assert (rlat, rlon) != (round(lat, 5), round(lon, 5))


def test_rounding_is_a_fixed_grid():
    # Two antennas a few metres apart, well inside one cell, round alike.
    a = round_position(33.4212, -111.9387, 1000)
    b = round_position(33.4213, -111.9386, 1000)
    assert a == b
    assert round_position(33.4212345, -111.9387654, 0) == (33.42123, -111.93877)
    assert round_position(10.0, 179.999, 100_000)[1] <= 180
