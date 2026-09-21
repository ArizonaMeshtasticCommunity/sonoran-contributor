"""Thinning: count the points in and out."""

from collections import Counter

from rf_agent.beast import Tracker
from rf_agent.messages import Counts, observation
from rf_agent.thinning import VISIT_GAP_S, Thinner

from conftest import SITE, replay_beast


def point(t, ident="a1b2c3", kind="adsb"):
    return observation(kind, ident, t, 33.0, -112.0)


def run(thinner, times, ident="a1b2c3"):
    out = []
    for t in times:
        out += thinner.offer(point(t, ident))
    return out


def test_one_point_per_interval_plus_first_and_last():
    counts = Counts()
    thinner = Thinner(20, counts)
    # One point a second for 200 s: t = 0 .. 199.
    sent = run(thinner, range(200))
    assert [p.t for p in sent] == [0, 20, 40, 60, 80, 100, 120, 140, 160, 180]
    assert thinner.expire(199 + VISIT_GAP_S) == []  # not silent long enough yet
    last = thinner.expire(199 + VISIT_GAP_S + 1)
    assert [p.t for p in last] == [199]
    assert counts.heard == 200
    assert counts.dropped == 200 - 11
    assert counts.heard == len(sent) + len(last) + counts.dropped
    assert len(thinner) == 0


def test_last_point_is_not_sent_twice():
    counts = Counts()
    thinner = Thinner(20, counts)
    sent = run(thinner, [0, 10, 20])
    assert [p.t for p in sent] == [0, 20]
    assert thinner.expire(1000) == []
    assert counts.heard == 3 and counts.dropped == 1


def test_a_gap_starts_a_new_visit():
    counts = Counts()
    thinner = Thinner(20, counts)
    sent = run(thinner, [0, 5, 10])
    # Heard again after a silence longer than a visit, before `expire` ran.
    sent += run(thinner, [100, 101])
    assert [p.t for p in sent] == [0, 10, 100]
    assert counts.heard == 5 and counts.dropped == 1


def test_aircraft_are_thinned_apart():
    counts = Counts()
    thinner = Thinner(20, counts)
    out = []
    for t in range(40):
        out += thinner.offer(point(t, "a1a1a1"))
        out += thinner.offer(point(t, "b2b2b2"))
        out += thinner.offer(point(t, "a1a1a1", kind="uat"))
    assert Counter((p.kind, p.id) for p in out) == {
        ("adsb", "a1a1a1"): 2,
        ("adsb", "b2b2b2"): 2,
        ("uat", "a1a1a1"): 2,
    }


def test_an_older_point_from_a_second_source_is_dropped():
    counts = Counts()
    thinner = Thinner(20, counts)
    run(thinner, [0, 25])
    assert run(thinner, [24]) == []
    assert counts.dropped == 1


def test_flush_sends_every_held_point():
    counts = Counts()
    thinner = Thinner(20, counts)
    run(thinner, [0, 3], "a1a1a1")
    run(thinner, [0], "b2b2b2")
    assert [(p.id, p.t) for p in thinner.flush()] == [("a1a1a1", 3)]
    assert len(thinner) == 0


def test_beast_capture_thinned(beast_capture):
    counts = Counts()
    thinner = Thinner(20, counts)
    sent = []
    for p in replay_beast(Tracker("adsb", SITE), beast_capture):
        sent += thinner.offer(p)
    sent += thinner.flush()
    per = Counter(p.id for p in sent)
    assert counts.heard == 816
    assert counts.heard == len(sent) + counts.dropped
    # 200 s at 20 s: ten points and the last one.
    assert per["a11a2b"] == 11
    # Two visits, each with its own first and last point.
    assert per["c0ffee"] == 4 + 5
    assert len(sent) < counts.heard / 10
