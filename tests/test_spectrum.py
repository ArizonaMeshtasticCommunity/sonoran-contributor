"""The spectrum summary, against the rtl_power capture.

A known carrier must land in the right bin, and a bursty one must raise
its `busy` (design 10).
"""

from datetime import datetime

import pytest

from rf_agent.spectrum import Grid, SpectrumSummary, parse_range, summarise

from conftest import EPOCH, FIXTURES, assert_valid

STEADY_BIN = (915_000_000 - 902_000_000) // 50_000  # 260
BURST_BIN = (906_875_000 - 902_000_000) // 50_000  # 97
QUIET_BIN = 400


def capture_lines() -> list[str]:
    return (FIXTURES / "rtl_power.csv").read_text().splitlines()


def when(line: str) -> float:
    date, clock = (part.strip() for part in line.split(",")[:2])
    return datetime.fromisoformat(f"{date}T{clock}+00:00").timestamp()


def replay(summary: SpectrumSummary, lines: list[str]) -> list[dict]:
    """Feed each line at the time rtl_power stamped it, then end the last minute."""
    out = []
    for line in lines:
        now = EPOCH if line.startswith("#") else when(line)
        summary.feed((line + "\n").encode(), now)
        out += summary.tick(now)
    return out + summary.tick(EPOCH + 180)


def assert_whole(message: dict) -> None:
    assert_valid(message)
    # The one rule JSON Schema cannot state: one entry per bin in each.
    lengths = {len(message[k]) for k in ("p50", "p95", "peak", "busy")}
    assert len(lengths) == 1


def test_two_minutes_make_two_messages():
    messages = replay(SpectrumSummary("test-site"), capture_lines())
    assert [m["t"] for m in messages] == ["2026-09-22T18:00:00.000Z", "2026-09-22T18:01:00.000Z"]
    for m in messages:
        assert_whole(m)
        assert m["kind"] == "spectrum" and m["station"] == "test-site"
        assert (m["f_start_hz"], m["bin_hz"], m["gain_db"]) == (902_000_000, 50_000, 32.8)
        assert m["sweeps"] == 12
        assert len(m["p50"]) == 520
        assert None not in m["p50"]


def test_a_steady_carrier_raises_the_median_of_its_bin_only():
    first = replay(SpectrumSummary("test-site"), capture_lines())[0]
    p50 = first["p50"]
    assert max(range(520), key=p50.__getitem__) == STEADY_BIN
    assert p50[STEADY_BIN] == pytest.approx(-380, abs=5)
    assert p50[QUIET_BIN] == pytest.approx(-500, abs=25)
    # Always on is its own median: steady, not busy.
    assert first["busy"][STEADY_BIN] == 0


def test_a_bursty_carrier_raises_busy_in_its_bin():
    first = replay(SpectrumSummary("test-site"), capture_lines())[0]
    busy = first["busy"]
    # On in 5 of the minute's 12 sweeps.
    assert busy[BURST_BIN] == 42
    assert max(range(520), key=busy.__getitem__) == BURST_BIN
    assert first["p95"][BURST_BIN] == pytest.approx(-300, abs=5)
    assert first["peak"][BURST_BIN] >= first["p95"][BURST_BIN]
    assert sum(1 for b in busy if b) <= 3


def test_bins_no_sweep_reached_are_none():
    lines = capture_lines()
    lines[0] = "# rf-sweep range=900000000:930000000:50000 gain_db=32.8"
    first = replay(SpectrumSummary("test-site"), lines)[0]
    assert_whole(first)
    assert first["p50"][:40] == [None] * 40  # 900 to 902 MHz
    assert first["busy"][39] is None
    assert first["p50"][40] is not None


def test_no_header_no_message():
    summary = SpectrumSummary("test-site")
    assert replay(summary, capture_lines()[1:]) == []


def test_a_header_without_gain_sends_nothing():
    lines = capture_lines()
    lines[0] = "# rf-sweep range=902000000:928000000:50000"
    assert replay(SpectrumSummary("test-site"), lines) == []


def test_a_new_header_sets_the_minute_aside():
    summary = SpectrumSummary("test-site")
    lines = capture_lines()
    replay_lines = [*lines[:100], "# rf-sweep range=902000000:928000000:50000 gain_db=40"]
    messages = replay(summary, replay_lines)
    assert messages == []  # the only minute began under another gain
    assert summary.gain_db == 40


def test_bad_lines_are_counted_not_raised():
    summary = SpectrumSummary("test-site")
    summary.feed(capture_lines()[0].encode() + b"\n", EPOCH)
    summary.feed(b"garbage\n2026-09-22, 18:00:00, x, y, z, 1, 2\n\xff\xfe\n", EPOCH)
    summary.feed(b"2026-09-22, 18:00:00, 902000000, 903000000, 0, 1, -50\n", EPOCH)
    assert summary.bad == 4
    summary.feed(b"2026-09-22, 18:00:00, 902000000, 903000000, 50000, 1, nan, -inf, -50\n", EPOCH)
    (message,) = summary.tick(EPOCH + 60)
    assert_whole(message)
    assert message["p50"][2] == -500 and message["p50"][0] is None


def test_summarise_one_bin():
    assert summarise([]) is None
    assert summarise([-50.0] * 10 + [-30.0] * 10) == (-400, -300, -300, 50)
    # The p95 of 20 values is the 19th.
    assert summarise([float(-v) for v in range(20)])[1] == -10
    assert summarise([-50.0] * 7 + [-40.0] * 3) == (-500, -400, -400, 30)
    assert summarise([500.0]) == (1000, 1000, 1000, 0)  # clamped to the contract


def test_parse_range():
    assert parse_range("902000000:928000000:50000") == Grid(902_000_000, 928_000_000, 50_000)
    assert Grid(902_000_000, 928_000_000, 50_000).bins == 520
    for bad in ("902:928", "928000000:902000000:50000", "902000000:928000000:0", "0:6000000000:1"):
        with pytest.raises(ValueError):
            parse_range(bad)
