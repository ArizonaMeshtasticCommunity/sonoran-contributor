"""Reading decoders over the network: reconnects, and bad input."""

import asyncio

from rf_agent.beast import Tracker
from rf_agent.sources import Backoff, poll, read_stream

from conftest import SITE


class Instant(Backoff):
    def next(self):
        super().next()
        return 0.01


async def test_stream_reconnects_when_the_decoder_restarts(beast_capture):
    connections = 0

    async def decoder(reader, writer):
        # Stands in for readsb. Only the test listens; the agent never does.
        nonlocal connections
        connections += 1
        writer.write(beast_capture[: len(beast_capture) // 2])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(decoder, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    got = []
    tracker = Tracker("adsb", SITE)
    task = asyncio.create_task(
        read_stream("adsb=beast", "127.0.0.1", port, tracker.feed, got.extend, backoff=Instant())
    )
    try:
        while connections < 3:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        server.close()
    assert got, "points from each connection"


async def test_stream_survives_a_parser_that_raises():
    async def decoder(reader, writer):
        writer.write(b"anything")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(b"more")
        await writer.drain()
        await asyncio.sleep(1)

    calls = []

    def handler(data, now):
        calls.append(data)
        raise RuntimeError("broken frame")

    server = await asyncio.start_server(decoder, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    task = asyncio.create_task(read_stream("x", "127.0.0.1", port, handler, lambda p: None))
    try:
        while len(calls) < 2:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        server.close()
    assert not task.done() or task.cancelled()


async def test_stream_retries_a_decoder_that_is_not_there():
    backoff = Instant()
    task = asyncio.create_task(
        read_stream("x", "127.0.0.1", 1, lambda d, n: [], None, backoff=backoff)
    )
    await asyncio.sleep(0.2)
    assert not task.done()
    task.cancel()
    assert backoff.delay > 1


async def test_poll_retries_then_reads(aircraft_snapshots):
    answers = [OSError("refused"), ValueError("bad url"), aircraft_snapshots[0]]

    def fetcher(url):
        answer = answers.pop(0) if answers else aircraft_snapshots[1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    from rf_agent.aircraft_json import AircraftJson

    got = []
    reader = AircraftJson()
    task = asyncio.create_task(
        poll("adsb=http", "http://readsb/data/aircraft.json", reader.parse, got.extend,
             every_s=0.01, backoff=Instant(), fetcher=fetcher)
    )  # fmt: skip
    try:
        while not got:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert {p.id for p in got} >= {"a4b5c6", "~2b3c4d"}


def test_backoff_doubles_to_a_minute():
    backoff = Backoff()
    delays = [backoff.next() for _ in range(9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    backoff.reset()
    assert backoff.next() == 1
