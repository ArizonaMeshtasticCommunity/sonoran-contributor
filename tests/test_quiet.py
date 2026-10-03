"""A failure that lasts is logged when it starts or changes, not on every retry.

On 2026-10-03 a member saw syslog fill up while a dongle was unplugged:
each retry logged the same lines again. These tests run each retry loop
against a failure that does not go away, and count what reaches the log.
"""

import asyncio
import logging
import sys

import pytest

from rf_agent.quiet import Condition
from rf_agent.sources import Backoff, read_stream
from rf_agent.sweep import Broadcaster, run_sweeper

LOG = logging.getLogger("test.quiet")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def lines(caplog, level=logging.INFO, name=None):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= level and (name is None or r.name.startswith(name))
    ]


def test_a_condition_logs_changes_and_an_hourly_reminder(caplog):
    clock = Clock()
    down = Condition(LOG, "adsb", clock=clock)
    with caplog.at_level(logging.INFO, logger="test.quiet"):
        down.ok()  # nothing was wrong: silent
        for _ in range(1000):
            down.bad("Connection refused")
            clock.now += 1
        down.bad("No route to host")  # changed: logged
        clock.now += 3600
        down.bad("No route to host")  # an hour on: one reminder
        down.ok("reading again")
        down.ok("reading again")  # already fine: silent
    assert lines(caplog) == [
        "adsb: Connection refused",
        "adsb: No route to host",
        "adsb: still No route to host (1002 times in 76 min)",
        "adsb: reading again after 76 min (1002 failures)",
    ]


async def test_a_decoder_that_is_down_is_one_warning_not_one_per_retry(caplog):
    # A port nothing listens on: every connect is refused.
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(
            read_stream(
                "adsb=beast://x",
                "127.0.0.1",
                port,
                lambda d, t: [],
                lambda p: None,
                backoff=Backoff(first=0.001, most=0.002),
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    retries = sum("retry in" in m for m in lines(caplog, logging.DEBUG))
    assert retries > 20, "the control: it really retried many times"
    warnings = lines(caplog, logging.WARNING, "rf_")
    assert len(warnings) == 1, warnings


async def test_a_decoder_that_accepts_and_closes_backs_off(caplog):
    """Before, each accepted connection reset the backoff: a tight loop."""
    connections = 0

    async def slam(reader, writer):
        nonlocal connections
        connections += 1
        writer.close()

    server = await asyncio.start_server(slam, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            read_stream(
                "uat=tcp://x",
                "127.0.0.1",
                port,
                lambda d, t: [],
                lambda p: None,
                backoff=Backoff(first=0.01, most=10),
            )
        )
        await asyncio.sleep(1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    server.close()
    # 0.01 + 0.02 + 0.04 + ... : about seven attempts in a second, not hundreds.
    assert 2 <= connections <= 9
    assert len(lines(caplog, logging.WARNING, "rf_")) == 1
    assert lines(caplog, logging.INFO, "rf_").count("uat=tcp://x: connected") == 1


async def test_rtl_power_failing_at_start_logs_its_lines_once(caplog):
    """The unplugged dongle: rtl_power prints its error and exits, again and again."""
    command = [
        sys.executable,
        "-c",
        "import sys; print('Found 0 device(s):', file=sys.stderr);"
        " print('No supported devices found.', file=sys.stderr); sys.exit(1)",
    ]
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            run_sweeper(command, Broadcaster(b""), stop, healthy_s=5, first_delay=0.01)
        )
        await asyncio.sleep(2.5)
        stop.set()
        await task
    logged = lines(caplog, logging.INFO, "rf_")
    assert logged.count("rtl_power: No supported devices found.") == 1, logged
    warnings = lines(caplog, logging.WARNING, "rf_")
    assert len(warnings) == 1 and "No supported devices found." in warnings[0]


async def test_rtl_power_that_recovers_says_so_once(caplog):
    marker = "import os, sys, time\n"
    script = (
        marker
        + "p = sys.argv[1]\n"
        + "n = int(open(p).read()) if os.path.exists(p) else 0\n"
        + "open(p, 'w').write(str(n + 1))\n"
        + "print('usb_open error -4', file=sys.stderr)\n"
        + "if n < 3: sys.exit(1)\n"
        + "time.sleep(30)\n"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        command = [sys.executable, "-c", script, f"{tmp}/n"]
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO):
            task = asyncio.create_task(
                run_sweeper(command, Broadcaster(b""), stop, healthy_s=0.5, first_delay=0.01)
            )
            await asyncio.sleep(2.0)
            stop.set()
            await task
    logged = lines(caplog, logging.INFO, "rf_")
    assert sum("sweeping again" in m for m in logged) == 1, logged
    assert len(lines(caplog, logging.WARNING, "rf_")) == 1


@pytest.mark.parametrize("seconds,text", [(5, "5 s"), (600, "10 min"), (9000, "2 h 30 min")])
def test_durations_read_plainly(seconds, text):
    from rf_agent.quiet import _duration

    assert _duration(seconds) == text
