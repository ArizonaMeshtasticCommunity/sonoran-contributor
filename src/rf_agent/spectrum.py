"""Condense a minute of sweeps into one spectrum message per range (design 3.5).

`rf-sweep` runs `rtl_power` and serves its CSV lines. Each line is one
hop of a sweep:

    date, time, Hz low, Hz high, Hz step, samples, dB, dB, dB, ...

The lines of one sweep share a date and time. `rf-sweep` starts each
connection with a header naming the band and the fixed gain, and sends
another each time it moves to another range:

    # rf-sweep range=902000000:928000000:50000 gain_db=32.8 name=ism

Before it, a plan line names every range it sweeps and the exact command
for each, as JSON, for the heartbeat:

    # rf-sweep-plan [{"tool": "rtl_power", "f_start_hz": ..., "command": ...}]

Each `rtl_power` bin goes to the bin of the header's grid its centre
falls in. `rtl_power` picks its own bin width, a little under the one
asked for, so a grid bin takes the loudest of the bins that fall in it.

At the end of each minute, per grid bin: the median, the 95th
percentile and the peak level, in tenths of a dB, and `busy`, the share
of sweeps in percent in which the bin stood more than 6 dB above its own
median. A bin no sweep reached is None. Minutes are the agent's clock.

A sweep exactly the same as the one before it is not counted. A live
receiver never repeats itself: noise moves every bin a little from one
sweep to the next. On 2026-10-02 a station's dongle stalled, and for 38
minutes `rtl_power` printed the same sweep over and over. The agent sent
it as real minutes: a flat band with no activity, which reads as a quiet
band rather than a dead receiver. Now a repeat is dropped, a minute with
no fresh sweep is not sent, and the log says so, so the map shows a gap.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from rf_agent.dump978 import LineSplitter
from rf_agent.messages import VERSION, Observation, timestamp
from rf_agent.quiet import Condition

logger = logging.getLogger(__name__)

HEADER = "# rf-sweep"
# The line that says what rf-sweep runs, all its ranges, as JSON (#324).
PLAN = "# rf-sweep-plan "
MAX_BINS = 4096
# The ranges one summary keeps at once: the contract's limit on the
# sweeps a heartbeat reports.
MAX_RANGES = 8
# A range's name, as the contract allows it.
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")
BUSY_DB = 6.0
# Minutes in a row with no sweep at all before it is reported: rf-sweep
# is up but rtl_power is not sweeping, most often an unplugged dongle.
EMPTY_MINUTES = 3
LEVEL_MIN, LEVEL_MAX = -2000, 1000  # tenths of a dB, as the contract allows


@dataclass(frozen=True)
class Grid:
    start: int
    end: int
    bin: int

    @property
    def bins(self) -> int:
        return math.ceil((self.end - self.start) / self.bin)

    def __str__(self) -> str:
        return f"{self.start}:{self.end}:{self.bin}"


def parse_range(text: str) -> Grid:
    """Parse `start:end:bin` in Hz. Raises ValueError."""
    try:
        start, end, step = (int(part) for part in text.strip().split(":"))
    except ValueError as exc:
        raise ValueError(f"{text!r} is not start:end:bin in Hz") from exc
    if not 0 <= start < end <= 6_000_000_000:
        raise ValueError(f"{text!r}: start must be below end, within 0 to 6 GHz")
    if not 1 <= step <= 10_000_000:
        raise ValueError(f"{text!r}: the bin must be between 1 Hz and 10 MHz")
    grid = Grid(start, end, step)
    if grid.bins > MAX_BINS:
        raise ValueError(f"{text!r} makes {grid.bins} bins; at most {MAX_BINS}")
    return grid


def _tenths(db: float) -> int:
    return max(LEVEL_MIN, min(LEVEL_MAX, round(db * 10)))


def summarise(samples: list[float]) -> tuple[int, int, int, int] | None:
    """p50, p95, peak (tenths of a dB) and busy (percent) of one bin's minute."""
    if not samples:
        return None
    ordered = sorted(samples)
    n = len(ordered)
    median = statistics.median(ordered)
    p95 = ordered[max(math.ceil(0.95 * n) - 1, 0)]
    busy = sum(1 for v in ordered if v > median + BUSY_DB)
    return _tenths(median), _tenths(p95), _tenths(ordered[-1]), round(100 * busy / n)


@dataclass
class _Range:
    """What one range has gathered in the current minute."""

    grid: Grid
    gain_db: float | None
    name: str | None = None
    key: tuple[str, str] | None = None
    sweep: dict[int, float] = field(default_factory=dict)
    samples: list[list[float]] = field(default_factory=list)
    sweeps: int = 0
    # The last sweep counted, to find a receiver that repeats itself.
    last_sweep: dict[int, float] | None = None
    repeats: int = 0

    def end_sweep(self) -> None:
        if not self.sweep:
            return
        if self.sweep == self.last_sweep:
            # The same levels in every bin as the sweep before: not a
            # measurement, a receiver that has stopped.
            self.repeats += 1
            self.sweep = {}
            return
        self.last_sweep = self.sweep
        if not self.samples:
            self.samples = [[] for _ in range(self.grid.bins)]
        for b, db in self.sweep.items():
            self.samples[b].append(db)
        self.sweeps += 1
        self.sweep = {}

    def reset(self) -> None:
        self.key = None
        self.sweep = {}
        self.samples = []
        self.sweeps = 0
        self.repeats = 0


class SpectrumSummary:
    """Read `rf-sweep` lines; produce one spectrum message per range per minute.

    `rf-sweep` may sweep more than one range, taking turns on one dongle
    (#324). It sends a header each time it moves to another range, and
    each range keeps its own minute here, so the minutes of two ranges
    are never mixed. A minute in which a range was not swept is not sent:
    Sonoran shows it as not swept, never as quiet.
    """

    def __init__(self, station: str) -> None:
        self.station = station
        self.lines = LineSplitter()
        self.bad = 0
        self._ranges: dict[Grid, _Range] = {}
        self._active: _Range | None = None
        self._minute: int | None = None
        self._ready: list[dict[str, Any]] = []
        self._warned_no_header = False
        # What rf-sweep says it runs, for the heartbeat (#324).
        self.sweeps: list[dict[str, Any]] = []
        # Minutes not sent in a row because every sweep was a repeat.
        self.frozen_minutes = 0
        # Minutes in a row with no sweep at all.
        self.empty_minutes = 0
        # Each check already waits whole minutes before it reports, so
        # its alert is not delayed again.
        self.health = Condition(logger, "spectrum", alert_after_s=0)

    @property
    def grid(self) -> Grid | None:
        """The range being swept now."""
        return self._active.grid if self._active else None

    @property
    def gain_db(self) -> float | None:
        return self._active.gain_db if self._active else None

    def feed(self, data: bytes, now: float) -> list[Observation]:
        """Handle bytes from `rf-sweep`. Spectrum has no observations: see `tick`."""
        for line in self.lines.feed(data):
            try:
                self.line(line.decode("ascii"), now)
            except (ValueError, UnicodeDecodeError):
                self.bad += 1
        return []

    def line(self, text: str, now: float) -> None:
        if text.startswith(PLAN):
            self.sweeps = plan_sweeps(text[len(PLAN) :])
            return
        if text.startswith("#"):
            self._header(text)
            return
        self._roll(now)
        active = self._active
        if active is None:
            if not self._warned_no_header:
                logger.warning("spectrum: no rf-sweep header yet, so no band or gain; waiting")
                self._warned_no_header = True
            return
        parts = [part.strip() for part in text.split(",")]
        if len(parts) < 7:
            raise ValueError("not an rtl_power line")
        low, step = float(parts[2]), float(parts[4])
        if not step > 0:
            raise ValueError("bad bin width")
        key = (parts[0], parts[1])
        if key != active.key:
            active.end_sweep()
            active.key = key
        grid = active.grid
        for i, text_db in enumerate(parts[6:]):
            db = float(text_db)
            if not math.isfinite(db):
                continue
            centre = low + (i + 0.5) * step
            b = math.floor((centre - grid.start) / grid.bin)
            if 0 <= b < grid.bins and db > active.sweep.get(b, -math.inf):
                active.sweep[b] = db

    def tick(self, now: float) -> list[dict[str, Any]]:
        """Return every minute finished by `now`, as messages ready to publish."""
        self._roll(now)
        out, self._ready = self._ready, []
        return out

    def _header(self, text: str) -> None:
        if not text.startswith(HEADER):
            return
        fields = dict(part.split("=", 1) for part in text[len(HEADER) :].split() if "=" in part)
        grid = parse_range(fields["range"]) if "range" in fields else None
        gain = float(fields["gain_db"]) if "gain_db" in fields else None
        name = fields.get("name")
        name = name if name and NAME.match(name) else None
        if self._active is not None:
            # A sweep cut short by the move to another range still counts.
            self._active.end_sweep()
            self._active.key = None
        if grid is None:
            self._active = None
            return
        state = self._ranges.get(grid)
        if state is None or state.gain_db != gain or state.name != name:
            # A new range, or a new gain: what was gathered under the old
            # one is not comparable, so it is set aside.
            logger.info(
                "spectrum: rf-sweep sweeps %s Hz at a fixed %s dB%s",
                grid,
                gain,
                f' ("{name}")' if name else "",
            )
            state = _Range(grid, gain, name)
            self._ranges[grid] = state
            while len(self._ranges) > MAX_RANGES:
                oldest = next(iter(self._ranges))
                del self._ranges[oldest]
        self._active = state

    def _roll(self, now: float) -> None:
        minute = int(now // 60)
        if self._minute is None:
            self._minute = minute
        elif minute > self._minute:
            self._ready += self._close(self._minute)
            self._minute = minute

    def _note_health(self, sweeps: int, repeats: int) -> None:
        """Report a receiver that repeats itself, or sends nothing, and its return."""
        if repeats and not sweeps:
            self.frozen_minutes += 1
            self.health.bad(
                "rf-sweep sends the same sweep over and over; the dongle may have stalled. "
                "Minutes are not sent until it changes"
            )
        elif not sweeps:
            self.empty_minutes += 1
            if self.empty_minutes >= EMPTY_MINUTES:
                self.health.bad(
                    f"no sweeps from rf-sweep for {EMPTY_MINUTES} minutes or more; "
                    "is the dongle plugged in?"
                )
        else:
            if self.health.failing:
                self.health.ok(
                    f"fresh sweeps again after {self.frozen_minutes + self.empty_minutes} "
                    "minutes not sent"
                )
            self.frozen_minutes = self.empty_minutes = 0
            if repeats:
                logger.debug("spectrum: %d repeated sweeps dropped this minute", repeats)

    def _close(self, minute: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        sweeps = repeats = 0
        for state in self._ranges.values():
            state.end_sweep()
            state.key = None
            sweeps += state.sweeps
            repeats += state.repeats
            message = self._message(state, minute)
            if message is not None:
                out.append(message)
            state.reset()
        self._note_health(sweeps, repeats)
        return out

    def _message(self, state: _Range, minute: int) -> dict[str, Any] | None:
        if not state.sweeps:
            return None
        if state.gain_db is None:
            logger.warning("spectrum: rf-sweep named no gain; a minute is not sent")
            return None
        stats = [summarise(samples) for samples in state.samples]
        message: dict[str, Any] = {
            "v": VERSION,
            "station": self.station,
            "kind": "spectrum",
            "t": timestamp(minute * 60),
            "f_start_hz": state.grid.start,
            "bin_hz": state.grid.bin,
            "sweeps": state.sweeps,
            "gain_db": state.gain_db,
            "p50": [s[0] if s else None for s in stats],
            "p95": [s[1] if s else None for s in stats],
            "peak": [s[2] if s else None for s in stats],
            "busy": [s[3] if s else None for s in stats],
        }
        if state.name:
            message["name"] = state.name
        return message


# What a heartbeat may say about one sweep, and the longest text in it.
SWEEP_KEYS = {
    "tool": str,
    "tool_version": str,
    "name": str,
    "f_start_hz": int,
    "f_end_hz": int,
    "bin_hz": int,
    "gain_db": float,
    "interval_s": int,
    "ppm": int,
    "dwell_s": int,
    "command": str,
}
MAX_SWEEPS = 8
MAX_COMMAND_CHARS = 512
MAX_PLAN_CHARS = 8192


def plan_sweeps(text: str) -> list[dict[str, Any]]:
    """Read the sweeps `rf-sweep` reports it runs. Bad entries are left out.

    rf-sweep is the member's own container, beside the agent. Its plan is
    still checked here, and again by Sonoran, which treats it as a claim.
    """
    if len(text) > MAX_PLAN_CHARS:
        return []
    try:
        items = json.loads(text)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items[:MAX_SWEEPS]:
        if not isinstance(item, dict):
            continue
        clean: dict[str, Any] = {}
        for key, kind in SWEEP_KEYS.items():
            value = item.get(key)
            if value is None or isinstance(value, bool):
                continue
            if kind is float and isinstance(value, int | float) and math.isfinite(value):
                clean[key] = float(value)
            elif kind is int and isinstance(value, int):
                clean[key] = value
            elif kind is str and isinstance(value, str):
                clean[key] = value[:MAX_COMMAND_CHARS] if key == "command" else value[:64]
        if {"tool", "f_start_hz", "f_end_hz", "bin_hz", "gain_db"} <= clean.keys():
            out.append(clean)
    return out
