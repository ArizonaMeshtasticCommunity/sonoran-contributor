"""tools/shift_capture.py moves every position, and keeps nothing it cannot move."""

import importlib.util
import json
from collections import Counter

import pytest

from rf_agent.beast import Tracker
from rf_agent.geo import distance_m

from conftest import ROOT, SITE, replay_beast

_spec = importlib.util.spec_from_file_location("shift", ROOT / "tools" / "shift_capture.py")
shift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shift)

DLAT, DLON = 0.731, -1.217


def test_beast_positions_move_by_the_shift(beast_capture):
    before = list(replay_beast(Tracker("adsb", SITE), beast_capture))
    moved_data, dropped = shift.shift_beast(beast_capture, DLAT, DLON, SITE)
    new_site = (SITE[0] + DLAT, SITE[1] + DLON)
    after = list(replay_beast(Tracker("adsb", new_site), moved_data))
    # The first half of each visit's first pair (four visits) could not
    # be moved, and is dropped, with the 29 frames with bad parity.
    assert dropped == 4 + 29
    by_id = Counter(p.id for p in after)
    assert by_id["a11a2b"] == 398 and by_id["a0b0c0"] == 60
    for p in after:
        assert distance_m(*new_site, p.lat, p.lon) < 100_000
    old_last = {p.id: p for p in before}
    new_last = {p.id: p for p in after}
    for ident, old in old_last.items():
        new = new_last[ident]
        assert new.lat == pytest.approx(old.lat + DLAT, abs=1e-4)
        assert new.lon == pytest.approx(old.lon + DLON, abs=1e-4)
        assert new.meta == old.meta


def test_beast_surface_frames_are_dropped_without_the_station(beast_capture):
    moved_data, _ = shift.shift_beast(beast_capture, DLAT, DLON, None)
    after = list(replay_beast(Tracker("adsb", (SITE[0] + DLAT, SITE[1] + DLON)), moved_data))
    assert "a0b0c0" not in {p.id for p in after}


def test_json_captures_move(aircraft_snapshots, dump978_capture):
    line = aircraft_snapshots[0].decode()
    out = json.loads(shift.shift_aircraft_json(line, DLAT, DLON, 3600))
    src = json.loads(line)
    assert out["now"] == src["now"] + 3600
    for a, b in zip(src["aircraft"], out["aircraft"], strict=True):
        if "lat" in a:
            assert b["lat"] == pytest.approx(a["lat"] + DLAT)
            assert b["lon"] == pytest.approx(a["lon"] + DLON)
    first = dump978_capture.decode().splitlines()[0]
    moved = json.loads(shift.shift_dump978(first, DLAT, DLON, -60))
    orig = json.loads(first)
    assert moved["position"]["lat"] == pytest.approx(orig["position"]["lat"] + DLAT)
    assert moved["metadata"]["received_at"] == pytest.approx(orig["metadata"]["received_at"] - 60)
