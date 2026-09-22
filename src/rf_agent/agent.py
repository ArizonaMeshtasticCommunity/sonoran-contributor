"""Run the agent: readers, thinning, and the publisher, in one event loop.

    decoders --> readers --> Thinner --> Outbox --> Publisher --> broker

Everything runs on one asyncio loop, so the pieces share state without
locks. On SIGTERM the readers stop, every visit's last point is queued,
and the publisher gets a few seconds to send it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time

from rf_agent import __version__
from rf_agent.aircraft_json import AircraftJson
from rf_agent.beast import Tracker
from rf_agent.config import Config, ConfigError, Source, load
from rf_agent.dump978 import Dump978
from rf_agent.messages import Counts, Observation
from rf_agent.publisher import Outbox, Publisher
from rf_agent.publisher import run as run_publisher
from rf_agent.sources import Handler, poll, read_stream
from rf_agent.spectrum import SpectrumSummary
from rf_agent.thinning import Thinner

logger = logging.getLogger("rf_agent")

EXPIRE_S = 1.0
SHUTDOWN_S = 5.0


class Pipeline:
    """Readers put points in; thinning decides what goes to the outbox."""

    def __init__(self, config: Config) -> None:
        self.counts = Counts()
        self.thinner = Thinner(config.adsb_interval_s, self.counts)
        self.outbox = Outbox()
        self.summaries: list[SpectrumSummary] = []

    def accept(self, points: list[Observation]) -> None:
        for point in points:
            for out in self.thinner.offer(point):
                self.outbox.add(out)

    def expire(self, now: float) -> None:
        for out in self.thinner.expire(now):
            self.outbox.add(out)
        for summary in self.summaries:
            for message in summary.tick(now):
                self.outbox.post("spectrum", message)

    def flush(self) -> None:
        for out in self.thinner.flush():
            self.outbox.add(out)


def handler_for(source: Source, config: Config, pipeline: Pipeline) -> Handler:
    """The parser for one source. Each source has its own state."""
    if source.reader == "rtl_power":
        summary = SpectrumSummary(config.station_id)
        pipeline.summaries.append(summary)
        return summary.feed
    if source.reader == "beast":
        return Tracker(source.kind, config.station).feed
    if source.reader == "dump978":
        return Dump978(source.kind).feed
    return AircraftJson(source.kind).parse


def reader_task(source: Source, config: Config, pipeline: Pipeline) -> asyncio.Task[None]:
    handler = handler_for(source, config, pipeline)
    if source.reader == "aircraft_json":
        coro = poll(source.name, source.url, handler, pipeline.accept)
    else:
        coro = read_stream(source.name, source.host, source.port, handler, pipeline.accept)
    return asyncio.create_task(coro, name=source.name)


async def _expire_loop(pipeline: Pipeline) -> None:
    while True:
        await asyncio.sleep(EXPIRE_S)
        pipeline.expire(time.time())


async def main(config: Config) -> None:
    pipeline = Pipeline(config)
    publisher = Publisher(config, pipeline.outbox, pipeline.counts)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    logger.info(
        "rf-agent %s, station %s, sources: %s",
        __version__,
        config.station_id,
        ", ".join(s.name for s in config.sources),
    )
    for kind in config.ignored_kinds:
        logger.warning("SOURCES: kind %r is not built yet; it is ignored", kind)

    readers = [reader_task(source, config, pipeline) for source in config.sources]
    readers.append(asyncio.create_task(_expire_loop(pipeline), name="expire"))
    # Set only once the last points are queued, so the final flush has them.
    drained = asyncio.Event()
    sender = asyncio.create_task(run_publisher(publisher, drained), name="publisher")

    await stop.wait()
    logger.info("stopping")
    for task in readers:
        task.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    pipeline.flush()
    drained.set()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(sender, SHUTDOWN_S)
    if pipeline.outbox:
        logger.warning("stopped with %d points unsent", len(pipeline.outbox))


def run() -> None:
    """Console entry point, `rf-agent`."""
    try:
        config = load(os.environ)
    except ConfigError as exc:
        print(f"rf-agent: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main(config))
