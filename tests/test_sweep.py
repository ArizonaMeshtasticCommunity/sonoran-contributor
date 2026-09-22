"""rf-sweep: its settings, and serving a sweep to readers across restarts."""

import asyncio
import sys

import pytest

from rf_agent.sources import read_stream
from rf_agent.spectrum import SpectrumSummary
from rf_agent.sweep import Broadcaster, load, run_sweeper

from conftest import FIXTURES

GOOD = {"SWEEP_SERIAL": "SWEEP915", "SWEEP_GAIN_DB": "32.8"}


def test_settings_and_the_command():
    config = load(GOOD)
    assert config.grid.bins == 520 and config.port == 30950
    assert config.header() == b"# rf-sweep range=902000000:928000000:50000 gain_db=32.8\n"
    command = config.command()
    assert command[0] == "rtl_power" and command[-1] == "-"
    assert command[command.index("-d") + 1] == "SWEEP915"
    assert command[command.index("-g") + 1] == "32.8"
    assert command[command.index("-f") + 1] == "902000000:928000000:50000"


@pytest.mark.parametrize(
    "changes,needle",
    [
        ({"SWEEP_SERIAL": ""}, "SWEEP_SERIAL"),
        ({"SWEEP_SERIAL": "1"}, "digits"),
        ({"SWEEP_GAIN_DB": ""}, "automatic"),
        ({"SWEEP_GAIN_DB": "auto"}, "automatic"),
        ({"SWEEP_GAIN_DB": "99"}, "SWEEP_GAIN_DB"),
        ({"SPECTRUM_RANGE": "928000000:902000000:50000"}, "SPECTRUM_RANGE"),
        ({"SWEEP_INTERVAL_S": "0"}, "SWEEP_INTERVAL_S"),
        ({"SWEEP_PORT": "http"}, "SWEEP_PORT"),
    ],
)
def test_bad_settings_are_named(changes, needle):
    with pytest.raises(ValueError, match=needle):
        load({**GOOD, **changes})


async def test_readers_get_the_header_and_every_line_across_restarts():
    # Stands in for rtl_power: prints the capture's first sweep, then exits.
    sweep = (FIXTURES / "rtl_power.csv").read_text().splitlines()[1:15]
    script = "import sys\nfor line in sys.argv[1:]: print(line)\n"
    command = [sys.executable, "-c", script, *sweep]

    broadcast = Broadcaster(b"# rf-sweep range=902000000:928000000:50000 gain_db=32.8\n")
    server = await asyncio.start_server(broadcast.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    got = bytearray()

    def handler(data, now):
        got.extend(data)
        return []

    reader = asyncio.create_task(read_stream("spectrum", "127.0.0.1", port, handler, None))
    while not broadcast.readers:
        await asyncio.sleep(0.01)
    stop = asyncio.Event()
    sweeper = asyncio.create_task(run_sweeper(command, broadcast, stop))
    try:
        # The command exits after one sweep and is started again.
        while got.count(b"\n") < 1 + 2 * len(sweep):
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(sweeper, 10)
        reader.cancel()
        server.close()
    lines = got.decode().splitlines()
    assert lines[0].startswith("# rf-sweep")
    assert lines[1 : 1 + len(sweep)] == sweep
    summary = SpectrumSummary("test-site")
    summary.feed(bytes(got), 0.0)
    assert summary.grid is not None and summary.bad == 0


async def test_a_missing_program_is_retried_not_fatal():
    stop = asyncio.Event()
    task = asyncio.create_task(run_sweeper(["no-such-rtl-power"], Broadcaster(b""), stop))
    await asyncio.sleep(0.2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_child_left_holding_the_pipes_does_not_stall_stopping():
    # The shell is stopped; the `sleep` it started keeps its pipes open.
    stop = asyncio.Event()
    command = ["sh", "-c", "sleep 30 & echo started; sleep 30"]
    task = asyncio.create_task(run_sweeper(command, Broadcaster(b""), stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 8)
