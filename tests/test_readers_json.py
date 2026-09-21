"""The aircraft.json and dump978 readers, against their captures."""

import json
import random
from collections import Counter

import pytest

from rf_agent.aircraft_json import AircraftJson
from rf_agent.dump978 import Dump978, LineSplitter
from rf_agent.messages import batch

from conftest import assert_valid


def read_snapshots(snapshots, kind="adsb"):
    reader = AircraftJson(kind)
    points = []
    for n, snap in enumerate(snapshots):
        points += reader.parse(snap, 0.0 + n)
    return reader, points


def test_aircraft_json_takes_only_new_positions_heard_here(aircraft_snapshots):
    reader, points = read_snapshots(aircraft_snapshots)
    per = Counter(p.id for p in points)
    # One per snapshot for aircraft whose position is fresh each second;
    # one per three for `abcdef`, whose position repeats in between.
    assert per == {"a4b5c6": 90, "~2b3c4d": 90, "a9c8d7": 90, "abcdef": 30, "a3a3a3": 90}
    # MLAT, by type and by field list, never passes.
    assert reader.excluded == 2 * 90
    assert "a7a7a7" not in per and "a55555" not in per


def test_aircraft_json_fields(aircraft_snapshots):
    _, points = read_snapshots(aircraft_snapshots[:1])
    got = {p.id: p for p in points}
    a = got["a4b5c6"]
    assert a.alt_m == pytest.approx(3658) and a.alt_ref == "baro"
    assert (a.gs_kn, a.track_deg, a.rssi_db) == (180.2, 60.1, -18.4)
    assert a.meta == {"callsign": "TST303", "squawk": "4521", "category": "A1"}
    snapshot_time = json.loads(aircraft_snapshots[0])["now"]
    assert a.t == pytest.approx(snapshot_time - 0.2)
    ground = got["a9c8d7"]
    assert ground.alt_m is None and ground.meta == {"ground": True}
    legacy = got["a3a3a3"]
    assert legacy.alt_m == pytest.approx(1829) and legacy.gs_kn == 140


def test_aircraft_json_bad_input():
    reader = AircraftJson()
    with pytest.raises(ValueError):
        reader.parse(b"not json", 0)
    with pytest.raises(ValueError):
        reader.parse(b'{"now": 1}', 0)
    doc = {
        "now": 100.0,
        "aircraft": [
            "not an object",
            {"hex": "zzzzzz", "lat": 1, "lon": 2},
            {"hex": "a1a1a1", "lat": 91, "lon": 2},
            {"hex": "a2a2a2", "lat": "x", "lon": 2},
            {"hex": "a3a3a3", "lat": 1, "lon": 2, "seen_pos": 90},
            {"hex": "a4a4a4", "lat": 1, "lon": 2, "alt_baro": 99999999, "track": 360},
        ],
    }
    points = reader.parse(json.dumps(doc).encode(), 0)
    assert [p.id for p in points] == ["a4a4a4"]
    assert points[0].alt_m is None and points[0].track_deg == 0.0
    assert reader.bad == 3


def test_dump978_lines(dump978_capture):
    reader = Dump978("uat")
    rng = random.Random(5)
    points, pos = [], 0
    while pos < len(dump978_capture):
        size = rng.randint(1, 400)
        points += reader.feed(dump978_capture[pos : pos + size], 0.0)
        pos += size
    per = Counter(p.id for p in points)
    assert per == {"a6b7c8": 90, "~1a2b3c": 90}  # no ground vehicle
    first = next(p for p in points if p.id == "a6b7c8")
    assert first.alt_ref == "baro" and first.alt_m == pytest.approx(1676)
    assert first.meta == {"callsign": "N123TS", "squawk": "1200", "category": "A1"}
    # The callsign is carried to positions whose message lacks it.
    assert all(p.meta.get("callsign") == "N123TS" for p in points if p.id == "a6b7c8")
    trackfile = next(p for p in points if p.id == "~1a2b3c")
    assert trackfile.alt_ref == "geom" and trackfile.rssi_db == -27.0
    assert reader.bad == 0
    for start in range(0, len(points), 100):
        assert_valid(batch("test-site", "uat", points[start : start + 100], 1_790_100_000.0))


def test_dump978_bad_and_raw_lines():
    reader = Dump978("uat")
    data = b"-0b28c9e1a2b3c4d5e;rs=1;\n{broken\n[1,2]\n\n" + b"x" * 70_000
    assert reader.feed(data, 0.0) == []
    assert reader.raw_lines == 1
    assert reader.bad == 2
    assert reader.lines.too_long == 1


def test_line_splitter_keeps_partial_lines():
    lines = LineSplitter()
    assert lines.feed(b"ab") == []
    assert lines.feed(b"c\nde") == [b"abc"]
    assert lines.feed(b"f\r\n") == [b"def"]
