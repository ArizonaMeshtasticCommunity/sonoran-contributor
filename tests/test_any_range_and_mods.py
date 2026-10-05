"""Any range a member chooses, what a sweeper reports, and members' mods (#324)."""

import asyncio
import json
import sys

import pytest

from rf_agent.agent import Pipeline, handler_for
from rf_agent.config import CUSTOM_PORT
from rf_agent.config import load as load_agent
from rf_agent.custom import CustomInput
from rf_agent.messages import Counts, custom_batch, custom_point, status
from rf_agent.publisher import Publisher
from rf_agent.spectrum import PLAN, SpectrumSummary, plan_sweeps
from rf_agent.sweep import (
    Broadcaster,
    Watch,
    _relay,
    load,
    parse_hz,
    run_sweeper,
    schedule_for,
)

from conftest import EPOCH, assert_valid

GOOD = {"SWEEP_SERIAL": "SWEEP915", "SWEEP_GAIN_DB": "32.8"}
TWO = {**GOOD, "SPECTRUM_RANGES": "902M:928M:25k:name=ism, 420M:450M:25k:name=uhf"}
AGENT = {
    "STATION_ID": "test-site",
    "BROKER_URL": "mqtts://broker.example:8883",
    "SOURCES": "spectrum=tcp://rf-sweep:30950",
}


# --- rf-sweep: ranges and limits ------------------------------------------


def test_frequencies_take_a_suffix():
    assert parse_hz("902M") == 902_000_000
    assert parse_hz("433.05M") == 433_050_000
    assert parse_hz("25k") == 25_000
    assert parse_hz("1.2G") == 1_200_000_000
    assert parse_hz("915000000") == 915_000_000
    with pytest.raises(ValueError):
        parse_hz("loud")


def test_several_ranges_with_names():
    config = load(TWO)
    assert [(r.grid.start, r.grid.end, r.grid.bin, r.name) for r in config.ranges] == [
        (902_000_000, 928_000_000, 25_000, "ism"),
        (420_000_000, 450_000_000, 25_000, "uhf"),
    ]
    assert config.takes_turns and config.dwell_s == 60
    assert config.header(1) == (
        b"# rf-sweep range=420000000:450000000:25000 gain_db=32.8 name=uhf dwell_s=60\n"
    )
    # One range is as it always was: no dwell, no name.
    assert load(GOOD).header() == b"# rf-sweep range=902000000:928000000:50000 gain_db=32.8\n"


@pytest.mark.parametrize(
    "changes,needle",
    [
        ({"SPECTRUM_RANGE": "902M:928M:50k"}, "not both"),
        ({"SPECTRUM_RANGES": "2400M:2500M:1M"}, "outside what an RTL-SDR can tune"),
        ({"SPECTRUM_RANGES": "10M:20M:10k"}, "outside what an RTL-SDR can tune"),
        ({"SPECTRUM_RANGES": "902M:928M:25k,902M:928M:25k"}, "twice"),
        ({"SPECTRUM_RANGES": "902M:928M:25k:name=Not OK"}, "name"),
        ({"SPECTRUM_RANGES": ",".join(f"{100 + i}M:{101 + i}M:10k" for i in range(9))}, "at most"),
        ({"SPECTRUM_DWELL_S": "5"}, "SPECTRUM_DWELL_S"),
    ],
)
def test_ranges_a_dongle_cannot_sweep_are_refused_at_start(changes, needle):
    with pytest.raises(ValueError, match=needle):
        load({**TWO, **changes} if "SPECTRUM_RANGE" not in changes else {**GOOD, **TWO, **changes})


def test_the_plan_reports_the_exact_command_but_the_serial():
    config = load(TWO)
    line = config.plan_line().decode()
    assert line.startswith(PLAN) and line.endswith("\n")
    plan = json.loads(line[len(PLAN) :])
    assert [p["name"] for p in plan] == ["ism", "uhf"]
    first = plan[0]
    assert first["command"] == (
        "rtl_power -d '<serial>' -f 902000000:928000000:25000 -g 32.8 -i 5 -p 0 -c 20% -"
    )
    assert "SWEEP915" not in line
    assert first["dwell_s"] == 60 and first["interval_s"] == 5
    # The agent reads it back as it will report it.
    assert plan_sweeps(line[len(PLAN) :].strip()) == plan


def test_turns_are_kept_to_the_wall_clock():
    config = load(TWO)
    assert config.slot(EPOCH - EPOCH % 120) == (0, 60.0)
    assert config.slot(EPOCH - EPOCH % 120 + 75) == (1, 45.0)
    assert load(GOOD).slot(EPOCH) == (0, None)


async def test_a_sweeper_takes_turns_and_readers_hear_each_header():
    # Stands in for rtl_power: one line naming its range, then waits.
    script = (
        "import sys, time\n"
        "print('2026-10-05, 12:00:00, ' + sys.argv[1] + ', 1, 1, 1, -50', flush=True)\n"
        "time.sleep(30)\n"
    )
    turns = iter(range(100))

    def schedule():
        index = next(turns) % 2
        command = [sys.executable, "-c", script, f"range{index}"]
        return command, f"# rf-sweep range=r{index}\n".encode(), 0.4, watches[index]

    watches = [Watch(), Watch()]
    broadcast = Broadcaster(b"# rf-sweep range=r0\n")
    sent: list[bytes] = []
    broadcast.send = sent.append  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_sweeper([sys.executable], broadcast, stop, healthy_s=0.1, schedule=schedule)
    )
    try:
        for _ in range(200):
            if sum(1 for line in sent if line.startswith(b"# rf-sweep")) >= 3:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
    headers = [line for line in sent if line.startswith(b"# rf-sweep")]
    assert headers[:3] == [
        b"# rf-sweep range=r1\n",
        b"# rf-sweep range=r0\n",
        b"# rf-sweep range=r1\n",
    ]
    assert any(b"range0" in line for line in sent) and any(b"range1" in line for line in sent)


async def test_repeats_are_counted_across_a_ranges_turns():
    """A turn is fewer sweeps than the limit; the count carries over."""
    watch = Watch()
    sweep = b"2026-10-05, 12:00:%02d, 902000000, 903000000, 50000, 1, -50\n"

    async def turn(start: int) -> str | None:
        reader = asyncio.StreamReader()
        for i in range(start, start + 3):
            reader.feed_data(sweep % i)
        reader.feed_eof()
        return await _relay(reader, Broadcaster(b""), 5, 60, None, watch)

    assert await turn(0) is None
    assert await turn(10) is None
    assert await turn(20) == "repeats"


def test_the_schedule_names_each_range_in_turn(monkeypatch):
    config = load(TWO)
    clock = iter([EPOCH - EPOCH % 120 + 1, EPOCH - EPOCH % 120 + 61])
    monkeypatch.setattr("rf_agent.sweep.time.time", lambda: next(clock))
    schedule = schedule_for(config)
    first, second = schedule(), schedule()
    assert first[1].startswith(b"# rf-sweep range=902000000") and first[2] == 59
    assert second[1].startswith(b"# rf-sweep range=420000000")
    assert first[3] is not second[3]


# --- rf-agent: a minute per range ---------------------------------------


def _sweep_lines(stamp: str, start: int, level: float) -> list[str]:
    levels = ", ".join(str(level - i) for i in range(4))
    return [f"2026-10-05, {stamp}, {start}, {start + 100_000}, 25000, 1, {levels}"]


def test_two_ranges_in_one_minute_make_two_messages_never_mixed():
    summary = SpectrumSummary("test-site")
    base = 1_790_000_000.0 - 1_790_000_000.0 % 60
    feed = lambda text, at: summary.feed((text + "\n").encode(), at)  # noqa: E731
    feed("# rf-sweep range=902000000:902100000:25000 gain_db=30 name=ism dwell_s=30", base)
    for i, second in enumerate(("00:00", "00:05", "00:10")):
        for line in _sweep_lines(f"12:{second}", 902_000_000, -50 - i):
            feed(line, base + i * 5)
    feed("# rf-sweep range=420000000:420100000:25000 gain_db=30 name=uhf dwell_s=30", base + 30)
    for i, second in enumerate(("00:30", "00:35")):
        for line in _sweep_lines(f"12:{second}", 420_000_000, -70 - i):
            feed(line, base + 30 + i * 5)
    messages = summary.tick(base + 61)
    assert [(m["f_start_hz"], m["name"], m["sweeps"]) for m in messages] == [
        (902_000_000, "ism", 3),
        (420_000_000, "uhf", 2),
    ]
    for message in messages:
        assert_valid(message)
        assert len(message["p50"]) == 4
    assert messages[1]["p50"][0] == -705


def test_a_range_not_swept_in_a_minute_sends_nothing():
    summary = SpectrumSummary("test-site")
    base = 1_790_000_000.0 - 1_790_000_000.0 % 60
    summary.feed(b"# rf-sweep range=902000000:902100000:25000 gain_db=30\n", base)
    summary.feed(b"# rf-sweep range=420000000:420100000:25000 gain_db=30\n", base)
    for line in _sweep_lines("12:00:01", 420_000_000, -60):
        summary.feed((line + "\n").encode(), base + 1)
    for line in _sweep_lines("12:00:06", 420_000_000, -61):
        summary.feed((line + "\n").encode(), base + 6)
    assert [m["f_start_hz"] for m in summary.tick(base + 61)] == [420_000_000]


def test_a_bad_plan_is_left_out_and_a_good_one_kept():
    assert plan_sweeps("not json") == []
    assert plan_sweeps('{"tool": "rtl_power"}') == []
    good = {"tool": "rtl_power", "f_start_hz": 1, "f_end_hz": 2, "bin_hz": 1, "gain_db": 3}
    assert plan_sweeps(json.dumps([good, {"tool": "x"}, "y"])) == [{**good, "gain_db": 3.0}]
    assert (
        plan_sweeps("[" + ",".join([json.dumps(good)] * 20) + "]") == [{**good, "gain_db": 3.0}] * 8
    )


def test_the_heartbeat_carries_what_the_sweeper_reports():
    config = load_agent(AGENT)
    pipeline = Pipeline(config)
    feed = handler_for(config.sources[0], config, pipeline)
    feed(load(TWO).plan_line(), EPOCH)
    publisher = Publisher(config, pipeline.outbox, pipeline.counts, clock=lambda: EPOCH)
    publisher.sweeps = pipeline.sweeps
    beat = publisher.heartbeat_message(Counts())
    assert_valid(beat)
    assert [s["name"] for s in beat["sweeps"]] == ["ism", "uhf"]
    # Spectrum is not an observation kind; nor is a mod.
    assert beat["kinds"] == []


def test_a_heartbeat_without_sweeps_is_as_before():
    beat = status(
        "test-site",
        EPOCH,
        kinds=["adsb"],
        counts=Counts(),
        agent_version="x",
        position=None,
        precision_m=1000,
    )
    assert "sweeps" not in beat
    assert_valid(beat)


# --- Members' mods -------------------------------------------------------


def test_a_mod_source_listens_on_this_machine_by_default():
    config = load_agent({**AGENT, "SOURCES": "custom=udp://"})
    (source,) = config.sources
    assert (source.reader, source.host, source.port) == ("custom", "127.0.0.1", CUSTOM_PORT)
    assert config.kinds == []
    with pytest.raises(ValueError, match="over UDP"):
        load_agent({**AGENT, "SOURCES": "custom=tcp://mod:1"})


def test_the_library_builds_points_and_batches_that_pass_the_contract():
    points = [
        custom_point("Strike-1", EPOCH, heard_here=True, meta={"distance_km": 12}),
        custom_point("car-7", EPOCH, lat=33.5, lon=-112.1, meta={"speed": 30, "x": {"y": 1}}),
        custom_point("note", EPOCH),
    ]
    assert all(points)
    assert points[0] == {
        "id": "strike-1",
        "t": "2026-09-22T18:00:00.000Z",
        "heard_here": True,
        "meta": {"distance_km": 12},
    }
    assert points[1]["meta"] == {"speed": 30}
    message = custom_batch("test-site", "lightning", points, EPOCH, source="pi5")
    assert_valid(message)
    # Not a point: a bad id, half a position, a position out of range.
    assert custom_point("Not An Id", EPOCH) is None
    assert custom_point("a", EPOCH, lat=33.5) is None
    assert custom_point("a", EPOCH, lat=95, lon=0) is None
    with pytest.raises(ValueError):
        custom_batch("test-site", "Not OK", [], EPOCH)


def _line(**item):
    return (json.dumps(item) + "\n").encode()


def test_a_mods_lines_are_checked_thinned_and_batched():
    mod = CustomInput("test-site", interval_s=10, per_minute=600)
    data = b"".join(
        [
            _line(mod="lightning", id="s1", t=EPOCH, heard_here=True),
            _line(mod="lightning", id="s1", t=EPOCH),  # within 10 s: thinned
            _line(mod="lightning", id="s2", t="2026-09-22T17:20:00Z", lat=33.5, lon=-112.0),
            _line(mod="other", id="o1"),
            _line(mod="Bad Mod", id="x"),
            _line(mod="lightning", id="<script>"),
            b"not json\n",
            b"[1, 2]\n",
        ]
    )
    assert mod.feed(data, EPOCH) == []
    assert (mod.bad, mod.thinned) == (4, 1)
    messages = mod.tick(EPOCH)
    assert [(m["mod"], [p["id"] for p in m["points"]]) for m in messages] == [
        ("lightning", ["s1", "s2"]),
        ("other", ["o1"]),
    ]
    for message in messages:
        assert_valid(message)
    assert mod.tick(EPOCH) == []


def test_a_mod_is_held_to_its_points_a_minute():
    mod = CustomInput("test-site", interval_s=1, per_minute=3)
    for i in range(5):
        mod.feed(_line(mod="busy", id=f"p{i}"), EPOCH)
    assert len(mod.tick(EPOCH)[0]["points"]) == 3 and mod.over == 2


def test_the_pipeline_posts_a_mods_batches():
    config = load_agent({**AGENT, "SOURCES": "custom=udp://127.0.0.1:30960"})
    pipeline = Pipeline(config)
    feed = handler_for(config.sources[0], config, pipeline)
    feed(_line(mod="lightning", id="s1", heard_here=True), EPOCH)
    pipeline.expire(EPOCH)
    ((kind, message),) = list(pipeline.outbox.posted)
    assert kind == "custom" and message["mod"] == "lightning"
    assert_valid(message)


def test_the_example_mod_sends_points_the_agent_takes():
    import importlib.util
    from argparse import Namespace

    from conftest import ROOT

    spec = importlib.util.spec_from_file_location("mod_stdin", ROOT / "examples" / "mod_stdin.py")
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    args = Namespace(
        mod="lightning",
        id_field="strike_id",
        lat_field="lat",
        lon_field="lon",
        heard_here=True,
        keep="distance_km,energy",
    )
    point = example.point_from('{"strike_id": 7, "distance_km": 12, "secret": "x"}', args)
    assert point["meta"] == {"distance_km": 12} and point["heard_here"] is True
    assert example.point_from("not json", args) is None
    mod = CustomInput("test-site")
    mod.feed((json.dumps(point) + "\n").encode(), EPOCH)
    (message,) = mod.tick(EPOCH)
    assert_valid(message)
