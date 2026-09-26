"""The Beast reader, against the Beast capture."""

import random
from collections import Counter

import pytest

from rf_agent.beast import MAX_RANGE_M, BeastFramer, Frame, Tracker, encode_frame, rssi_db
from rf_agent.geo import distance_m
from rf_agent.messages import batch

from conftest import EPOCH, SITE, assert_valid, replay_beast
from encode import es, position_me
from make_captures import moved


def frames_of(data: bytes, chunk_sizes=None) -> list[Frame]:
    framer = BeastFramer()
    if chunk_sizes is None:
        return list(framer.feed(data))
    out, pos = [], 0
    for size in chunk_sizes:
        out += framer.feed(data[pos : pos + size])
        pos += size
    return out + list(framer.feed(data[pos:]))


def test_framing_does_not_depend_on_how_reads_split(beast_capture):
    whole = frames_of(beast_capture)
    rng = random.Random(7)
    sizes = [rng.randint(1, 60) for _ in range(len(beast_capture) // 20)]
    assert frames_of(beast_capture, sizes) == whole
    assert frames_of(beast_capture, [1] * len(beast_capture)) == whole
    kinds = Counter(f.kind for f in whole)
    assert set(kinds) == {0x31, 0x32, 0x33}


def test_escaped_bytes_round_trip():
    frame = Frame(0x33, 0x1A1A1A, 0x1A, bytes.fromhex("8da11a2b1a1a1a1a1a1a1a1a1a1a"))
    data = encode_frame(frame)
    assert data.count(b"\x1a") > 10
    assert list(BeastFramer().feed(data)) == [frame]


def test_garbage_and_a_frame_cut_short():
    good = encode_frame(Frame(0x32, 1, 99, bytes(7)))
    framer = BeastFramer()
    # A frame cut off by a new one: the lone 0x1a starts the next frame.
    data = b"junk" + good[:6] + good + b"\x1a\x39more" + good
    assert len(list(framer.feed(data))) == 2
    assert framer.skipped >= 2
    # Half a frame waits for the rest.
    assert list(framer.feed(good[:5])) == []
    assert len(list(framer.feed(good[5:]))) == 1


def test_capture_decodes_every_aircraft(beast_capture):
    tracker = Tracker("adsb", SITE)
    points = list(replay_beast(tracker, beast_capture))
    per = Counter(p.id for p in points)
    # Every position frame after the first even/odd pair of a visit.
    assert per == {"a11a2b": 399, "c0ffee": 238, "~abc123": 119, "a0b0c0": 60}
    assert tracker.bad == 29  # the frames with broken parity
    assert all(p.kind == "adsb" for p in points)


def test_positions_follow_the_true_track(beast_capture):
    points = [p for p in replay_beast(Tracker("adsb", SITE), beast_capture) if p.id == "a11a2b"]
    for p in points:
        t = round((p.t - EPOCH - 5_000_000 / 12_000_000) * 2) / 2
        lat, lon = moved(32.80, -113.72, 250, 90, t)
        assert distance_m(lat, lon, p.lat, p.lon) < 20


def test_fields_and_meta(beast_capture):
    points = list(replay_beast(Tracker("adsb", SITE), beast_capture))
    last = {p.id: p for p in points}
    a = last["a11a2b"]
    assert a.alt_m == pytest.approx(3048) and a.alt_ref == "baro"
    assert a.gs_kn == pytest.approx(250, abs=1) and a.track_deg == pytest.approx(90, abs=1)
    assert a.meta == {"callsign": "TST101", "squawk": "1200", "category": "A3"}
    assert a.rssi_db is not None and -50 < a.rssi_db < 0
    gnss = last["~abc123"]
    assert gnss.alt_ref == "geom" and gnss.alt_m == 1500
    ground = last["a0b0c0"]
    assert ground.meta == {"ground": True} and ground.alt_m is None
    assert ground.gs_kn == 9.5


def test_surface_positions_need_the_station(beast_capture):
    points = list(replay_beast(Tracker("adsb", None), beast_capture))
    assert "a0b0c0" not in {p.id for p in points}
    assert len(points) == 399 + 238 + 119


def test_positions_out_of_range_are_dropped(beast_capture):
    far = (SITE[0] + 8, SITE[1])  # about 890 km away
    assert distance_m(*far, *SITE) > MAX_RANGE_M
    tracker = Tracker("adsb", far)
    # Every airborne position is refused. A surface position is decoded
    # against the station itself, so it always lands near it: a station
    # set in the wrong place cannot be caught from surface frames.
    assert {p.id for p in replay_beast(tracker, beast_capture)} == {"a0b0c0"}


def test_a_jump_no_aircraft_makes_is_dropped():
    tracker = Tracker("adsb", SITE)
    frames = [
        es("a1a1a1", position_me(32.8, -113.3, False, 5000)),
        es("a1a1a1", position_me(32.8, -113.3, True, 5000)),
        # One second later, 100 km north.
        es("a1a1a1", position_me(33.7, -113.3, False, 5000)),
    ]
    got = [tracker.frame(Frame(0x33, 0, 100, f), 100.0 + n) for n, f in enumerate(frames)]
    assert got[0] is None and got[1] is not None
    assert got[2] is None
    assert tracker.bad == 1


def test_feed_never_raises_on_noise():
    tracker = Tracker("adsb", SITE)
    rng = random.Random(3)
    for _ in range(200):
        tracker.feed(rng.randbytes(rng.randint(0, 300)), 1000.0)
    tracker.feed(b"\x1a\x33" + bytes(21), 1000.0)


def test_forgets_old_aircraft(beast_capture):
    tracker = Tracker("adsb", SITE)
    list(replay_beast(tracker, beast_capture))
    assert tracker.aircraft
    tracker.forget(EPOCH + 10_000)
    assert not tracker.aircraft


def test_rssi():
    assert rssi_db(255) == 0.0
    assert rssi_db(0) is None
    assert rssi_db(26) == pytest.approx(-19.8, abs=0.1)


def test_every_point_fits_the_contract(beast_capture):
    points = list(replay_beast(Tracker("adsb", SITE), beast_capture))
    for start in range(0, len(points), 100):
        assert_valid(batch("test-site", "adsb", points[start : start + 100], EPOCH))


def test_a_position_carries_the_frame_it_came_from(beast_capture):
    points = list(replay_beast(Tracker("adsb", SITE), beast_capture))
    point = points[0]
    # Uppercase hex, no framing, as every Mode S tool prints a frame.
    assert point.raw is not None
    assert len(point.raw) == 28 and point.raw == point.raw.upper()
    assert bytes.fromhex(point.raw)[0] >> 3 in (17, 18)
