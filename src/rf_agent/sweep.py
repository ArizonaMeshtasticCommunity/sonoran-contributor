"""`rf-sweep`: sweep the 900 MHz band on a dongle of its own (design 3.5).

A spectrum sweep is the one source with no decoder to share: the sweeper
owns its dongle while it runs. So it runs in its own container, beside
the agent, on a dongle chosen by serial number, and does nothing else.
The agent never opens a device; it reads what this serves.

It runs `rtl_power` over the band at a **fixed gain**, never automatic,
so a week of readings can be compared with itself. It serves the output
lines over TCP to any number of readers, each connection starting with
a header that names the band and the gain:

    # rf-sweep range=902000000:928000000:50000 gain_db=32.8

If `rtl_power` stops, for a dongle unplugged or a USB error, it is
started again with backoff. Readers stay connected across it.

Settings, from the environment:

    SWEEP_SERIAL       the dongle's serial, set once with `rtl_eeprom -s`
    SWEEP_GAIN_DB      the fixed gain in dB, such as 32.8
    SPECTRUM_RANGE     start:end:bin in Hz, default 902000000:928000000:50000
    SWEEP_INTERVAL_S   seconds per sweep, default 5
    SWEEP_PPM          the dongle's frequency correction, default 0
    SWEEP_PORT         the port readers connect to, default 30950
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rf_agent.config import RF_SWEEP_PORT
from rf_agent.quiet import Condition
from rf_agent.spectrum import HEADER, Grid, parse_range

logger = logging.getLogger("rf_sweep")

DEFAULT_RANGE = "902000000:928000000:50000"
# `rtl_power` drops the edges of each hop, where the dongle's filter
# rolls off, and overlaps the hops to cover them.
CROP = "20%"
# A reader this far behind is dropped rather than let memory grow.
MAX_BACKLOG = 1 << 20
BACKOFF_MAX_S = 60.0


@dataclass(frozen=True)
class SweepConfig:
    serial: str
    gain_db: float
    grid: Grid
    interval_s: int = 5
    ppm: int = 0
    port: int = RF_SWEEP_PORT

    def header(self) -> bytes:
        return f"{HEADER} range={self.grid} gain_db={self.gain_db:g}\n".encode()

    def command(self) -> list[str]:
        return [
            "rtl_power",
            "-d", self.serial,
            "-f", f"{self.grid.start}:{self.grid.end}:{self.grid.bin}",
            "-g", f"{self.gain_db:g}",
            "-i", str(self.interval_s),
            "-p", str(self.ppm),
            "-c", CROP,
            "-",
        ]  # fmt: skip


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

    grid = parse_range(DEFAULT_RANGE)
    try:
        grid = parse_range(env.get("SPECTRUM_RANGE", "").strip() or DEFAULT_RANGE)
    except ValueError as exc:
        errors.append(f"SPECTRUM_RANGE: {exc}")

    numbers = {}
    for name, default, low, high in (
        ("SWEEP_INTERVAL_S", 5, 1, 60),
        ("SWEEP_PPM", 0, -200, 200),
        ("SWEEP_PORT", RF_SWEEP_PORT, 1, 65535),
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
    return SweepConfig(
        serial=serial,
        gain_db=gain,
        grid=grid,
        interval_s=numbers["SWEEP_INTERVAL_S"],
        ppm=numbers["SWEEP_PPM"],
        port=numbers["SWEEP_PORT"],
    )


class Broadcaster:
    """Send every line to every connected reader."""

    def __init__(self, header: bytes) -> None:
        self.header = header
        self.readers: set[asyncio.StreamWriter] = set()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("reader connected: %s", peer)
        writer.write(self.header)
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


async def run_sweeper(
    command: Sequence[str],
    broadcast: Broadcaster,
    stop: asyncio.Event,
    healthy_s: float = HEALTHY_S,
    first_delay: float = 1.0,
    repeat_limit: int = REPEAT_LIMIT,
) -> None:
    """Run the sweep command until `stop`, starting it again when it ends.

    With the dongle unplugged, rtl_power prints a few lines and exits at
    once, and is started again for as long as that lasts. Logging those
    lines on every start filled members' disks (2026-10-03). So a run's
    lines are held back until it has run `healthy_s`. A run that ends
    before then is a failure: its lines are logged only when the failure
    is new or different, and otherwise only counted (`quiet.Condition`).
    """
    delay = first_delay
    down = Condition(logger, command[0])
    while not stop.is_set():
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
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
            reading = asyncio.create_task(_relay(proc.stdout, broadcast, repeat_limit))
            stopping = asyncio.create_task(stop.wait())
            proving = asyncio.create_task(_prove(healthy_s, held, live, down))
            await asyncio.wait({reading, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            proving.cancel()
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
            stuck = not reading.cancelled() and reading.exception() is None and reading.result()
            if stuck:
                # Restarted on purpose: the same sweep over and over.
                down.bad(
                    f"sent the same sweep {repeat_limit} times; restarting it to find the dongle"
                )
                delay = first_delay
            elif live.is_set():
                # It worked for a while, then stopped: worth a line each time.
                logger.warning("%s ended with %s", command[0], proc.returncode)
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


async def _prove(healthy_s: float, held: list[str], live: asyncio.Event, down: Condition) -> None:
    """After `healthy_s`, log what the run printed so far and go live."""
    await asyncio.sleep(healthy_s)
    for text in held:
        logger.info("rtl_power: %s", text)
    held.clear()
    live.set()
    down.ok("sweeping again")


async def _relay(stream: asyncio.StreamReader, broadcast: Broadcaster, repeat_limit: int) -> bool:
    """Pass rtl_power's lines on. True when it stopped because it repeats itself.

    A sweep is the lines that share a date and time. Two sweeps are the
    same when every line is the same after the date and time.
    """
    key: bytes | None = None
    sweep: list[bytes] = []
    last: list[bytes] | None = None
    repeats = 0
    while line := await stream.readline():
        broadcast.send(line)
        parts = line.split(b",", 2)
        if len(parts) < 3 or line.startswith(b"#"):
            continue
        stamp, rest = parts[0] + b"," + parts[1], parts[2]
        if stamp != key:
            if sweep:
                repeats = repeats + 1 if sweep == last else 0
                last = sweep
                if repeat_limit and repeats >= repeat_limit:
                    return True
            key, sweep = stamp, []
        sweep.append(rest.strip())
    return False


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
    broadcast = Broadcaster(config.header())
    # It listens inside the compose network, for the agent. Do not publish
    # this port on the host.
    server = await asyncio.start_server(broadcast.serve, None, config.port)
    logger.info(
        "sweeping %s Hz at a fixed %g dB on dongle %s; serving on port %d",
        config.grid,
        config.gain_db,
        config.serial,
        config.port,
    )
    try:
        await run_sweeper(command or config.command(), broadcast, stop)
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
