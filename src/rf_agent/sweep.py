"""`rf-sweep`: sweep any range a member chooses, on a dongle of its own (design 3.5).

A spectrum sweep is the one source with no decoder to share: the sweeper
owns its dongle while it runs. So it runs in its own container, beside
the agent, on a dongle chosen by serial number, and does nothing else.
The agent never opens a device; it reads what this serves.

It runs `rtl_power` over the band at a **fixed gain**, never automatic,
so a week of readings can be compared with itself. It serves the output
lines over TCP to any number of readers, each connection starting with
a plan line that says what it runs (#324), then a header that names the
range and the gain:

    # rf-sweep-plan [{"tool": "rtl_power", ..., "command": "rtl_power -d <serial> ..."}]
    # rf-sweep range=902000000:928000000:50000 gain_db=32.8

It may sweep more than one range (#324). With `SPECTRUM_RANGES`, one
dongle takes turns: each range for `SPECTRUM_DWELL_S` seconds, the turns
kept to the wall clock, and a new header each time it moves on. A
dongle that takes turns between N ranges hears each one only 1/N of the
time. For two ranges at once, run two rf-sweep containers on two dongles.

If `rtl_power` stops, for a dongle unplugged or a USB error, it is
started again with backoff. Readers stay connected across it.

Settings, from the environment:

    SWEEP_SERIAL       the dongle's serial, set once with `rtl_eeprom -s`
    SWEEP_GAIN_DB      the fixed gain in dB, such as 32.8
    SPECTRUM_RANGE     start:end:bin, default 902000000:928000000:50000
    SPECTRUM_RANGES    several, comma separated, each start:end:bin[:name=x],
                       such as 902M:928M:25k:name=ism,420M:450M:25k:name=uhf
    SPECTRUM_DWELL_S   seconds on each range in turn, default 60
    SWEEP_INTERVAL_S   seconds per sweep, default 5
    SWEEP_PPM          the dongle's frequency correction, default 0
    SWEEP_PORT         the port readers connect to, default 30950

Frequencies take a k, M or G suffix. A range outside what an RTL-SDR can
tune is refused at start, never swept to silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from rf_agent.config import RF_SWEEP_PORT
from rf_agent.quiet import Condition
from rf_agent.spectrum import HEADER, MAX_RANGES, NAME, PLAN, Grid, parse_range

logger = logging.getLogger("rf_sweep")

DEFAULT_RANGE = "902000000:928000000:50000"
# `rtl_power` drops the edges of each hop, where the dongle's filter
# rolls off, and overlaps the hops to cover them.
CROP = "20%"
# A reader this far behind is dropped rather than let memory grow.
MAX_BACKLOG = 1 << 20
BACKOFF_MAX_S = 60.0


# What an RTL-SDR can tune: an R820T or R828D tuner, about 24 MHz to
# 1.766 GHz. rtl_power asked for more starts and returns nothing, which
# would read as a quiet band. Anything higher needs other hardware.
TUNER_LOW_HZ = 24_000_000
TUNER_HIGH_HZ = 1_766_000_000
DEFAULT_DWELL_S = 60
SUFFIX = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
HZ = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmMgG]?)\s*(?:hz|Hz|HZ)?\s*$")


def parse_hz(text: str) -> int:
    """A frequency in Hz, with an optional k, M or G. Raises ValueError."""
    match = HZ.match(text)
    if not match:
        raise ValueError(f"{text!r} is not a frequency")
    return round(float(match.group(1)) * SUFFIX[match.group(2).lower()])


@dataclass(frozen=True)
class SweepRange:
    """One range to sweep, and the name its member gave it."""

    grid: Grid
    name: str | None = None


def parse_ranges(text: str) -> tuple[SweepRange, ...]:
    """Parse `start:end:bin[:name=x]`, comma separated. Raises ValueError."""
    out: list[SweepRange] = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        parts = item.split(":")
        name = None
        if len(parts) == 4 and parts[3].startswith("name="):
            name = parts[3][len("name=") :].strip().lower()
            if not NAME.match(name):
                raise ValueError(f"{item!r}: a name is a-z, 0-9 and -, up to 16 characters")
            parts = parts[:3]
        if len(parts) != 3:
            raise ValueError(f"{item!r} is not start:end:bin, with an optional :name=")
        start, end, step = (parse_hz(part) for part in parts)
        out.append(SweepRange(parse_range(f"{start}:{end}:{step}"), name))
    if not out:
        raise ValueError("names no range")
    if len(out) > MAX_RANGES:
        raise ValueError(f"at most {MAX_RANGES} ranges")
    if len({r.grid for r in out}) != len(out):
        raise ValueError("a range is listed twice")
    return tuple(out)


@dataclass(frozen=True)
class SweepConfig:
    serial: str
    gain_db: float
    ranges: tuple[SweepRange, ...]
    interval_s: int = 5
    ppm: int = 0
    port: int = RF_SWEEP_PORT
    dwell_s: int = DEFAULT_DWELL_S
    # The rtl-sdr package's version, found at start, for the heartbeat.
    tool_version: str | None = field(default=None, compare=False)

    @property
    def grid(self) -> Grid:
        """The first range: the only one, for most stations."""
        return self.ranges[0].grid

    @property
    def takes_turns(self) -> bool:
        return len(self.ranges) > 1

    def header(self, index: int = 0) -> bytes:
        item = self.ranges[index]
        text = f"{HEADER} range={item.grid} gain_db={self.gain_db:g}"
        if item.name:
            text += f" name={item.name}"
        if self.takes_turns:
            text += f" dwell_s={self.dwell_s}"
        return f"{text}\n".encode()

    def command(self, index: int = 0) -> list[str]:
        grid = self.ranges[index].grid
        return [
            "rtl_power",
            "-d", self.serial,
            "-f", f"{grid.start}:{grid.end}:{grid.bin}",
            "-g", f"{self.gain_db:g}",
            "-i", str(self.interval_s),
            "-p", str(self.ppm),
            "-c", CROP,
            "-",
        ]  # fmt: skip

    def reported_command(self, index: int = 0) -> str:
        """The command as it is reported: exact, but for the dongle's serial."""
        command = self.command(index)
        command[command.index("-d") + 1] = "<serial>"
        return shlex.join(command)

    def plan(self) -> list[dict[str, Any]]:
        """What this sweeper runs, for the agent's heartbeat (#324)."""
        out: list[dict[str, Any]] = []
        for index, item in enumerate(self.ranges):
            entry: dict[str, Any] = {
                "tool": "rtl_power",
                "f_start_hz": item.grid.start,
                "f_end_hz": item.grid.end,
                "bin_hz": item.grid.bin,
                "gain_db": self.gain_db,
                "interval_s": self.interval_s,
                "ppm": self.ppm,
                "command": self.reported_command(index),
            }
            if self.tool_version:
                entry["tool_version"] = self.tool_version
            if item.name:
                entry["name"] = item.name
            if self.takes_turns:
                entry["dwell_s"] = self.dwell_s
            out.append(entry)
        return out

    def plan_line(self) -> bytes:
        return f"{PLAN}{json.dumps(self.plan(), separators=(',', ':'))}\n".encode()

    def slot(self, now: float) -> tuple[int, float | None]:
        """The range to sweep at wall-clock `now`, and seconds left on it.

        Turns are kept to the wall clock, so each range's minutes are the
        same minutes every hour, and a restart does not shift them.
        """
        if not self.takes_turns:
            return 0, None
        turn = int(now // self.dwell_s)
        return turn % len(self.ranges), (turn + 1) * self.dwell_s - now


def rtl_sdr_version() -> str | None:
    """The installed rtl-sdr package's version, or None."""
    try:
        done = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "rtl-sdr"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = done.stdout.strip()
    return version[:64] if done.returncode == 0 and version else None


def load(env: Mapping[str, str]) -> SweepConfig:
    """Read the settings. Raises ValueError naming every problem."""
    errors: list[str] = []
    serial = env.get("SWEEP_SERIAL", "").strip()
    if not serial:
        errors.append("SWEEP_SERIAL is required: the serial of the dongle this owns")
    elif serial.isdigit():
        # rtl_power reads a number as a device index before a serial, so
        # serial "1" would open the second dongle, whatever it serves.
        errors.append("SWEEP_SERIAL must not be all digits; set one with letters, rtl_eeprom -s")

    gain_text = env.get("SWEEP_GAIN_DB", "").strip()
    gain = 0.0
    try:
        gain = float(gain_text)
        if not math.isfinite(gain) or not 0 <= gain <= 60:
            errors.append("SWEEP_GAIN_DB must be between 0 and 60")
    except ValueError:
        errors.append("SWEEP_GAIN_DB is required, a fixed number of dB; automatic gain is refused")

    ranges = parse_ranges(DEFAULT_RANGE)
    one = env.get("SPECTRUM_RANGE", "").strip()
    many = env.get("SPECTRUM_RANGES", "").strip()
    setting = "SPECTRUM_RANGES" if many else "SPECTRUM_RANGE"
    if one and many:
        errors.append("set SPECTRUM_RANGE or SPECTRUM_RANGES, not both")
    else:
        try:
            ranges = parse_ranges(many or one or DEFAULT_RANGE)
        except ValueError as exc:
            errors.append(f"{setting}: {exc}")
    for item in ranges:
        if item.grid.start < TUNER_LOW_HZ or item.grid.end > TUNER_HIGH_HZ:
            errors.append(
                f"{setting}: {item.grid} Hz is outside what an RTL-SDR can tune, "
                f"{TUNER_LOW_HZ // 1_000_000} to {TUNER_HIGH_HZ // 1_000_000} MHz; "
                "rtl_power would sweep nothing. Higher ranges need other hardware"
            )

    numbers = {}
    for name, default, low, high in (
        ("SWEEP_INTERVAL_S", 5, 1, 60),
        ("SWEEP_PPM", 0, -200, 200),
        ("SWEEP_PORT", RF_SWEEP_PORT, 1, 65535),
        ("SPECTRUM_DWELL_S", DEFAULT_DWELL_S, 10, 3600),
    ):
        text = env.get(name, "").strip()
        try:
            numbers[name] = int(text) if text else default
            if not low <= numbers[name] <= high:
                errors.append(f"{name} must be between {low} and {high}")
        except ValueError:
            errors.append(f"{name} must be a whole number, not {text!r}")

    if errors:
        raise ValueError("; ".join(errors))
    if len(ranges) > 1 and numbers["SPECTRUM_DWELL_S"] < 2 * numbers["SWEEP_INTERVAL_S"]:
        errors.append("SPECTRUM_DWELL_S must allow at least two sweeps of each range")
        raise ValueError("; ".join(errors))
    return SweepConfig(
        serial=serial,
        gain_db=gain,
        ranges=ranges,
        interval_s=numbers["SWEEP_INTERVAL_S"],
        ppm=numbers["SWEEP_PPM"],
        port=numbers["SWEEP_PORT"],
        dwell_s=numbers["SPECTRUM_DWELL_S"],
    )


class Broadcaster:
    """Send every line to every connected reader.

    A reader that connects gets the plan, then the header of the range
    being swept now. When the sweeper moves to another range, every
    reader gets its header (`set_header`).
    """

    def __init__(self, header: bytes, plan: bytes = b"") -> None:
        self.header = header
        self.plan = plan
        self.readers: set[asyncio.StreamWriter] = set()

    def set_header(self, header: bytes) -> None:
        if header != self.header:
            self.header = header
            self.send(header)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("reader connected: %s", peer)
        writer.write(self.plan + self.header)
        self.readers.add(writer)
        try:
            # Readers send nothing. Wait for them to hang up.
            while await reader.read(1024):
                pass
        except OSError:
            pass
        finally:
            self.readers.discard(writer)
            writer.close()
            logger.info("reader gone: %s", peer)

    def send(self, line: bytes) -> None:
        for writer in list(self.readers):
            if writer.is_closing():
                self.readers.discard(writer)
            elif writer.transport.get_write_buffer_size() > MAX_BACKLOG:
                logger.warning("dropping a reader that is not keeping up")
                self.readers.discard(writer)
                writer.close()
            else:
                writer.write(line)


# A run of rtl_power that lasts this long is working: its own lines are
# logged from then on, and the backoff starts again from a second.
HEALTHY_S = 60.0
# The most stderr lines kept back from one run that has not proved itself.
MAX_HELD_LINES = 50
# Identical sweeps in a row before rtl_power is restarted: about a
# minute at the usual 5 s a sweep. Unplugged, rtl_power does not exit;
# it prints its last sweep forever and never opens the dongle again
# (2026-10-03). Only a restart finds a dongle plugged back in.
REPEAT_LIMIT = 12
# Seconds with no sweep line before rtl_power is restarted. Restarted
# with its dongle gone, rtl_power can sit and print nothing, and then
# never exits on its own (2026-10-03). A sweep takes about 5 s.
SILENT_S = 120.0


@dataclass
class Watch:
    """What the checks remember of one range, across its turns (#324).

    A dongle that takes turns sweeps a range for a minute at a time, which
    is fewer sweeps than `REPEAT_LIMIT` and less time than `SILENT_S`. So
    the counts are kept per range and carried from one turn to the next.
    """

    last: list[bytes] | None = None
    repeats: int = 0
    # Seconds of turns in a row in which this range printed no sweep.
    quiet_s: float = 0.0


# One turn: the command, the header that names its range, the seconds
# left on it (None: until it ends), and the range's watch.
Slot = tuple[Sequence[str], bytes, "float | None", Watch]


async def run_sweeper(
    command: Sequence[str],
    broadcast: Broadcaster,
    stop: asyncio.Event,
    healthy_s: float = HEALTHY_S,
    first_delay: float = 1.0,
    repeat_limit: int = REPEAT_LIMIT,
    silent_s: float = SILENT_S,
    schedule: Callable[[], Slot] | None = None,
) -> None:
    """Run the sweep command until `stop`, starting it again when it ends.

    With the dongle unplugged, rtl_power prints a few lines and exits at
    once, and is started again for as long as that lasts. Logging those
    lines on every start filled members' disks (2026-10-03). So a run's
    lines are held back until it has run `healthy_s`. A run that ends
    before then is a failure: its lines are logged only when the failure
    is new or different, and otherwise only counted (`quiet.Condition`).

    With a `schedule`, the sweeper takes turns between ranges (#324):
    each turn runs the command the schedule gives until the turn ends,
    then moves to the next range at once. A turn that ends on time is
    not a failure. A range that printed no sweep for `silent_s` of its
    turns is reported, and one that repeats itself `repeat_limit` times
    across its turns is restarted, as for one range.
    """
    delay = first_delay
    down = Condition(logger, command[0] if command else "rtl_power")
    single = Watch()
    while not stop.is_set():
        if schedule is not None:
            run, header, seconds, watch = schedule()
            broadcast.set_header(header)
        else:
            run, seconds, watch = command, None, single
        try:
            proc = await asyncio.create_subprocess_exec(
                *run,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            down.bad(f"cannot start: {exc}")
        else:
            assert proc.stdout is not None and proc.stderr is not None
            held: list[str] = []
            live = asyncio.Event()
            logs = asyncio.create_task(_log_lines(proc.stderr, held, live))
            swept = asyncio.Event()
            reading = asyncio.create_task(
                _relay(proc.stdout, broadcast, repeat_limit, silent_s, swept, watch)
            )
            stopping = asyncio.create_task(stop.wait())
            proving = asyncio.create_task(_prove(healthy_s, held, live, down, swept))
            waits = {reading, stopping}
            turn = None
            if seconds is not None:
                turn = asyncio.create_task(asyncio.sleep(max(0.0, seconds)))
                waits.add(turn)
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            proving.cancel()
            ended_turn = turn is not None and turn.done() and not reading.done()
            if turn is not None:
                turn.cancel()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 5)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            # Stop reading its pipes too. A child it left behind could hold
            # them open, and must not keep this waiting.
            reading.cancel()
            logs.cancel()
            await asyncio.gather(reading, logs, proving, return_exceptions=True)
            if stop.is_set():
                return
            if ended_turn:
                # Its turn is over: on to the next range, at once.
                if swept.is_set():
                    watch.quiet_s = 0.0
                    if down.failing:
                        down.ok("sweeping again")
                    delay = first_delay
                else:
                    watch.quiet_s += seconds or 0.0
                    if watch.quiet_s >= silent_s:
                        down.bad(f"printed no sweep for {watch.quiet_s:.0f} s of its turns")
                continue
            stuck = None
            if not reading.cancelled() and reading.exception() is None:
                stuck = reading.result()
            if stuck == "repeats":
                # Restarted on purpose: the same sweep over and over.
                watch.repeats = 0
                down.bad(
                    f"sent the same sweep {repeat_limit} times; restarting it to find the dongle"
                )
            elif stuck == "silent":
                # Restarted on purpose: running, and printing nothing. The
                # backoff keeps growing, so it keeps trying, more slowly,
                # until the dongle is back.
                down.bad(f"printed no sweep for {silent_s:.0f} s; restarting it")
            elif live.is_set():
                # It worked for a while, then stopped: worth a line each time.
                logger.warning("%s ended with %s", run[0], proc.returncode)
                delay = first_delay
            else:
                failure = f"ended with {proc.returncode} at start"
                if held:
                    failure += f": {held[-1]}"
                if failure != down.text:
                    # New, or changed: say what it printed, once.
                    for text in held:
                        logger.info("rtl_power: %s", text)
                down.bad(failure)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), delay)
        delay = min(delay * 2, BACKOFF_MAX_S)


def schedule_for(config: SweepConfig) -> Callable[[], Slot]:
    """The turns of a sweeper with more than one range, by the wall clock."""
    watches = [Watch() for _ in config.ranges]

    def next_slot() -> Slot:
        index, seconds = config.slot(time.time())
        return config.command(index), config.header(index), seconds, watches[index]

    return next_slot


async def _prove(
    healthy_s: float, held: list[str], live: asyncio.Event, down: Condition, swept: asyncio.Event
) -> None:
    """Once the run has lasted `healthy_s` and sent a sweep, log what it
    printed so far and go live. Running alone is not working: a silent
    rtl_power is not sweeping."""
    await asyncio.sleep(healthy_s)
    await swept.wait()
    for text in held:
        logger.info("rtl_power: %s", text)
    held.clear()
    live.set()
    down.ok("sweeping again")


async def _relay(
    stream: asyncio.StreamReader,
    broadcast: Broadcaster,
    repeat_limit: int,
    silent_s: float,
    swept: asyncio.Event | None = None,
    watch: Watch | None = None,
) -> str | None:
    """Pass rtl_power's lines on, until it ends or stops making sense.

    Returns "repeats" when it sends the same sweep `repeat_limit` times,
    "silent" when it sends no sweep line for `silent_s`, and None when
    it ends. A sweep is the lines that share a date and time; two are the
    same when every line is the same after the date and time. The count
    of repeats is kept in `watch`, so it carries across a range's turns.
    """
    watch = watch if watch is not None else Watch()
    key: bytes | None = None
    sweep: list[bytes] = []
    loop = asyncio.get_running_loop()
    heard = loop.time()
    while True:
        try:
            line = await asyncio.wait_for(
                stream.readline(), max(0.0, heard + silent_s - loop.time())
            )
        except TimeoutError:
            return "silent"
        if not line:
            return None
        broadcast.send(line)
        parts = line.split(b",", 2)
        if len(parts) < 3 or line.startswith(b"#"):
            continue
        heard = loop.time()
        if swept is not None:
            swept.set()
        stamp, rest = parts[0] + b"," + parts[1], parts[2]
        if stamp != key:
            if sweep:
                watch.repeats = watch.repeats + 1 if sweep == watch.last else 0
                watch.last = sweep
                if repeat_limit and watch.repeats >= repeat_limit:
                    return "repeats"
            key, sweep = stamp, []
        sweep.append(rest.strip())


async def _log_lines(stream: asyncio.StreamReader, held: list[str], live: asyncio.Event) -> None:
    while line := await stream.readline():
        text = line.decode(errors="replace").rstrip()
        if live.is_set():
            logger.info("rtl_power: %s", text)
        elif len(held) < MAX_HELD_LINES:
            held.append(text)


async def main(config: SweepConfig, command: Sequence[str] | None = None) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    if config.tool_version is None:
        config = replace(config, tool_version=await asyncio.to_thread(rtl_sdr_version))
    broadcast = Broadcaster(config.header(), config.plan_line())
    # It listens inside the compose network, for the agent. Do not publish
    # this port on the host.
    server = await asyncio.start_server(broadcast.serve, None, config.port)
    ranges = ", ".join(
        f"{item.grid}" + (f" ({item.name})" if item.name else "") for item in config.ranges
    )
    logger.info(
        "sweeping %s Hz at a fixed %g dB on dongle %s%s; serving on port %d",
        ranges,
        config.gain_db,
        config.serial,
        f", {config.dwell_s} s on each in turn" if config.takes_turns else "",
        config.port,
    )
    if config.takes_turns:
        logger.info(
            "taking turns between %d ranges: each is swept about 1/%d of the time",
            len(config.ranges),
            len(config.ranges),
        )
    schedule = schedule_for(config) if config.takes_turns and command is None else None
    try:
        await run_sweeper(
            command or config.command(),
            broadcast,
            stop,
            healthy_s=min(HEALTHY_S, config.dwell_s / 2) if schedule else HEALTHY_S,
            schedule=schedule,
        )
    finally:
        server.close()
        for writer in list(broadcast.readers):
            writer.close()


def run() -> None:
    """Console entry point, `rf-sweep`."""
    try:
        config = load(os.environ)
    except ValueError as exc:
        print(f"rf-sweep: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main(config))
