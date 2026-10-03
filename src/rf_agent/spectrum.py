"""Condense a minute of 900 MHz sweeps into one spectrum message (design 3.5).

`rf-sweep` runs `rtl_power` and serves its CSV lines. Each line is one
hop of a sweep:

    date, time, Hz low, Hz high, Hz step, samples, dB, dB, dB, ...

The lines of one sweep share a date and time. `rf-sweep` starts each
connection with a header naming the band and the fixed gain:

    # rf-sweep range=902000000:928000000:50000 gain_db=32.8

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

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Any

from rf_agent.dump978 import LineSplitter
from rf_agent.messages import VERSION, Observation, timestamp

logger = logging.getLogger(__name__)

HEADER = "# rf-sweep"
MAX_BINS = 4096
BUSY_DB = 6.0
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


class SpectrumSummary:
    """Read `rf-sweep` lines; produce one spectrum message per minute."""

    def __init__(self, station: str) -> None:
        self.station = station
        self.lines = LineSplitter()
        self.grid: Grid | None = None
        self.gain_db: float | None = None
        self.bad = 0
        self._key: tuple[str, str] | None = None
        self._sweep: dict[int, float] = {}
        self._samples: list[list[float]] = []
        self._sweeps = 0
        self._minute: int | None = None
        self._ready: list[dict[str, Any]] = []
        self._warned_no_header = False
        # The last sweep counted, to find a receiver that repeats itself.
        self._last_sweep: dict[int, float] | None = None
        self._repeats = 0
        # Minutes not sent in a row because every sweep was a repeat.
        self.frozen_minutes = 0

    def feed(self, data: bytes, now: float) -> list[Observation]:
        """Handle bytes from `rf-sweep`. Spectrum has no observations: see `tick`."""
        for line in self.lines.feed(data):
            try:
                self.line(line.decode("ascii"), now)
            except (ValueError, UnicodeDecodeError):
                self.bad += 1
        return []

    def line(self, text: str, now: float) -> None:
        if text.startswith("#"):
            self._header(text)
            return
        self._roll(now)
        if self.grid is None:
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
        if key != self._key:
            self._end_sweep()
            self._key = key
        grid = self.grid
        for i, text_db in enumerate(parts[6:]):
            db = float(text_db)
            if not math.isfinite(db):
                continue
            centre = low + (i + 0.5) * step
            b = math.floor((centre - grid.start) / grid.bin)
            if 0 <= b < grid.bins and db > self._sweep.get(b, -math.inf):
                self._sweep[b] = db

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
        if grid != self.grid or gain != self.gain_db:
            # A new band or gain: what was gathered under the old one is
            # not comparable, so it is set aside.
            self._reset()
            self._last_sweep = None
            logger.info("spectrum: rf-sweep sweeps %s Hz at a fixed %s dB", grid, gain)
        self.grid, self.gain_db = grid, gain

    def _roll(self, now: float) -> None:
        minute = int(now // 60)
        if self._minute is None:
            self._minute = minute
        elif minute > self._minute:
            message = self._close(self._minute)
            if message is not None:
                self._ready.append(message)
            self._minute = minute

    def _end_sweep(self) -> None:
        if not self._sweep:
            return
        if self._sweep == self._last_sweep:
            # The same levels in every bin as the sweep before: not a
            # measurement, a receiver that has stopped.
            self._repeats += 1
            self._sweep = {}
            return
        self._last_sweep = self._sweep
        if not self._samples and self.grid is not None:
            self._samples = [[] for _ in range(self.grid.bins)]
        for b, db in self._sweep.items():
            self._samples[b].append(db)
        self._sweeps += 1
        self._sweep = {}

    def _reset(self) -> None:
        self._key = None
        self._sweep = {}
        self._samples = []
        self._sweeps = 0
        self._repeats = 0

    def _note_repeats(self) -> None:
        """Log when the receiver starts and stops repeating itself."""
        if self._repeats and not self._sweeps:
            if not self.frozen_minutes:
                logger.warning(
                    "spectrum: rf-sweep sent the same sweep %d times and nothing new; "
                    "the dongle may have stalled. Minutes are not sent until it changes",
                    self._repeats + 1,
                )
            self.frozen_minutes += 1
        elif self._sweeps and self.frozen_minutes:
            logger.info(
                "spectrum: fresh sweeps again after %d minutes not sent", self.frozen_minutes
            )
            self.frozen_minutes = 0
        elif self._repeats:
            logger.debug("spectrum: %d repeated sweeps dropped this minute", self._repeats)

    def _close(self, minute: int) -> dict[str, Any] | None:
        self._end_sweep()
        self._key = None
        self._note_repeats()
        if not self._sweeps or self.grid is None or self.gain_db is None:
            if self._sweeps and self.gain_db is None:
                logger.warning("spectrum: rf-sweep named no gain; a minute is not sent")
            self._reset()
            return None
        stats = [summarise(samples) for samples in self._samples]
        message = {
            "v": VERSION,
            "station": self.station,
            "kind": "spectrum",
            "t": timestamp(minute * 60),
            "f_start_hz": self.grid.start,
            "bin_hz": self.grid.bin,
            "sweeps": self._sweeps,
            "gain_db": self.gain_db,
            "p50": [s[0] if s else None for s in stats],
            "p95": [s[1] if s else None for s in stats],
            "peak": [s[2] if s else None for s in stats],
            "busy": [s[3] if s else None for s in stats],
        }
        self._reset()
        return message
