"""The captures are what their generator writes, and stay near the fictional site."""

import json

from rf_agent.geo import distance_m

import make_captures
from conftest import EPOCH, FIXTURES, SITE


def test_captures_match_their_generator():
    assert make_captures.SITE == SITE
    assert make_captures.START == EPOCH
    assert (FIXTURES / "beast.bin").read_bytes() == make_captures.beast()
    assert (FIXTURES / "aircraft.jsonl").read_text() == make_captures.aircraft_json()
    assert (FIXTURES / "dump978.jsonl").read_text() == make_captures.dump978()


def test_json_captures_stay_near_the_site():
    for line in (FIXTURES / "aircraft.jsonl").read_text().splitlines():
        for ac in json.loads(line)["aircraft"]:
            if "lat" in ac:
                assert distance_m(*SITE, ac["lat"], ac["lon"]) < 100_000
