"""Publish to the broker: batches of observations, and the heartbeat.

Topics (design 4.1), for this station only:

    rf/v1/<station>/<kind>             batches of at most 100 points, QoS 0
    rf/v1/<station>/spectrum           a minute of one swept range, QoS 0
    rf/v1/<station>/custom             a batch from a member's mod, QoS 0
    rf/v1/<station>/status/<source>    the heartbeat, every 60 s, retained, QoS 1

With no `SOURCE_ID` the heartbeat goes to `rf/v1/<station>/status`, as
before Sonoran #305. A station with two Pis needs a source on each:
both heartbeats are retained, and on one topic each would replace the
other.

Points wait in the `Outbox` between flushes, and while the broker is
away. The outbox is bounded: when it is full the oldest points go
first, and the log says how many. A point lost that way is replaced by
the next one 20 s later, the same as a lost QoS 0 message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import aiomqtt

from rf_agent import __version__
from rf_agent.config import Config
from rf_agent.geo import round_position
from rf_agent.messages import MAX_OBS, Counts, Observation, batch, status
from rf_agent.quiet import Condition

logger = logging.getLogger(__name__)

HEARTBEAT_S = 60.0
FLUSH_S = 2.0
# About ten minutes of a busy site.
MAX_QUEUED = 10_000
# Whole messages waiting: two hours of four ranges' spectrum minutes,
# and members' mods' batches.
MAX_POSTED = 600
BACKOFF_MAX_S = 60.0


class Client(Protocol):
    async def publish(
        self, topic: str, payload: bytes, qos: int = 0, retain: bool = False
    ) -> None: ...


def topic(station: str, name: str) -> str:
    return f"rf/v1/{station}/{name}"


def status_topic(station: str, source: str | None) -> str:
    return topic(station, "status" if source is None else f"status/{source}")


def client_id(config: Config) -> str:
    """The MQTT client id. One per login and source.

    Two connections with one client id disconnect each other, over and
    over. Before Sonoran #305 it was the station id alone, so two Pis of
    one station knocked each other off the broker.
    """
    name = f"rf-agent-{config.broker_user}"
    if config.source_id and not config.broker_user.endswith(f"-{config.source_id}"):
        name += f"-{config.source_id}"
    return name


class Outbox:
    """Points waiting to be sent, per kind, oldest first."""

    def __init__(self, limit: int = MAX_QUEUED) -> None:
        self.limit = limit
        self._queues: dict[str, deque[Observation]] = {}
        # Whole messages built elsewhere, such as spectrum minutes, oldest
        # first. When full, the oldest goes.
        self.posted: deque[tuple[str, dict[str, Any]]] = deque(maxlen=MAX_POSTED)
        self.overflowed = 0

    def __len__(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def add(self, obs: Observation) -> None:
        queue = self._queues.setdefault(obs.kind, deque())
        queue.append(obs)
        if len(self) > self.limit:
            # Drop from the longest queue, so one busy kind cannot
            # push out another.
            longest = max(self._queues.values(), key=len)
            longest.popleft()
            self.overflowed += 1

    def take(self, kind: str) -> list[Observation]:
        """Remove and return up to MAX_OBS points of one kind."""
        queue = self._queues.get(kind)
        if not queue:
            return []
        return [queue.popleft() for _ in range(min(MAX_OBS, len(queue)))]

    def put_back(self, kind: str, obs: list[Observation]) -> None:
        """Return points that could not be sent, at the front."""
        queue = self._queues.setdefault(kind, deque())
        queue.extendleft(reversed(obs))

    def kinds(self) -> list[str]:
        return [kind for kind, queue in self._queues.items() if queue]

    def post(self, kind: str, message: dict[str, Any]) -> None:
        """Queue a whole message for `rf/v1/<station>/<kind>`."""
        self.posted.append((kind, message))


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode()


class Publisher:
    """Send what the outbox holds, and the heartbeat, over one connection."""

    def __init__(
        self,
        config: Config,
        outbox: Outbox,
        counts: Counts,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.outbox = outbox
        self.counts = counts
        self.clock = clock
        self.position = (
            round_position(*config.station, config.precision_m) if config.station else None
        )
        self._reported_overflow = 0
        # Called after each heartbeat the broker took: proof the session works.
        self.on_beat: Callable[[], None] | None = None
        # What the sweepers beside the agent say they run (#324).
        self.sweeps: Callable[[], list[dict[str, Any]]] = list

    async def flush(self, client: Client) -> int:
        """Publish every waiting point, in batches. Returns how many were sent.

        A failed publish puts its points back and raises, so the next
        connection sends them.
        """
        sent = 0
        station = self.config.station_id
        source = self.config.source_id
        while self.outbox.posted:
            kind, message = self.outbox.posted[0]
            if source is not None:
                message = {**message, "source": source}
            await client.publish(topic(station, kind), encode(message), qos=0, retain=False)
            self.outbox.posted.popleft()
        for kind in self.outbox.kinds():
            while chunk := self.outbox.take(kind):
                payload = encode(batch(station, kind, chunk, self.clock(), source))
                try:
                    await client.publish(topic(station, kind), payload, qos=0, retain=False)
                except BaseException:
                    self.outbox.put_back(kind, chunk)
                    raise
                sent += len(chunk)
                self.counts.sent += len(chunk)
        if self.outbox.overflowed != self._reported_overflow:
            logger.warning(
                "outbox full: %d points dropped oldest first while the broker was away",
                self.outbox.overflowed - self._reported_overflow,
            )
            self._reported_overflow = self.outbox.overflowed
        return sent

    def heartbeat_message(self, counts: Counts) -> dict[str, Any]:
        return status(
            self.config.station_id,
            self.clock(),
            kinds=self.config.kinds,
            counts=counts,
            agent_version=__version__,
            position=self.position,
            precision_m=self.config.precision_m,
            mesh_node=self.config.mesh_node,
            source=self.config.source_id,
            sweeps=self.sweeps(),
        )

    async def heartbeat(self, client: Client) -> Counts:
        """Publish the heartbeat with the counts since the last one."""
        counts = self.counts.take()
        payload = encode(self.heartbeat_message(counts))
        try:
            await client.publish(
                status_topic(self.config.station_id, self.config.source_id),
                payload,
                qos=1,
                retain=True,
            )
        except BaseException:
            self.counts.give_back(counts)
            raise
        if self.on_beat is not None:
            self.on_beat()
        logger.info(
            "heartbeat: heard %d, sent %d, thinned %d, waiting %d",
            counts.heard,
            counts.sent,
            counts.dropped,
            len(self.outbox),
        )
        return counts

    async def serve(self, client: Client, stop: asyncio.Event) -> None:
        """Flush every FLUSH_S and beat every HEARTBEAT_S until `stop` is set."""
        next_beat = 0.0
        while True:
            await self.flush(client)
            # A last heartbeat on the way out, so its counts are not lost.
            if time.monotonic() >= next_beat or stop.is_set():
                await self.heartbeat(client)
                next_beat = time.monotonic() + HEARTBEAT_S
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), FLUSH_S)
            except TimeoutError:
                pass


def make_client(config: Config) -> aiomqtt.Client:
    """A client for the broker, over TLS. It only connects out."""
    return aiomqtt.Client(
        hostname=config.broker_host,
        port=config.broker_port,
        username=config.broker_user or None,
        password=config.broker_pass or None,
        identifier=client_id(config),
        protocol=aiomqtt.ProtocolVersion.V311,
        clean_session=True,
        keepalive=60,
        tls_context=ssl.create_default_context(),
    )


async def run(
    publisher: Publisher,
    stop: asyncio.Event,
    connect: Callable[[Config], AbstractAsyncContextManager[Any]] = make_client,
    first_delay: float = 1.0,
) -> None:
    """Keep a connection to the broker and serve it, until `stop` is set.

    A broker that is down or refuses the login is retried with backoff,
    up to a minute apart. The agent never exits over it. The failure is
    logged when it starts or changes and then once an hour, not on each
    retry, and the recovery once a heartbeat has gone through.
    """
    delay = first_delay
    down = Condition(logger, "broker")
    first = True

    def beat_ok() -> None:
        nonlocal delay
        delay = first_delay
        down.ok("connected again")

    publisher.on_beat = beat_ok
    while True:
        try:
            async with connect(publisher.config) as client:
                if first and not down.failing:
                    logger.info(
                        "connected to broker %s:%d",
                        publisher.config.broker_host,
                        publisher.config.broker_port,
                    )
                first = False
                await publisher.serve(client, stop)
                return
        except (aiomqtt.MqttError, OSError) as exc:
            down.bad(str(exc))
        except Exception as exc:
            if not down.failing:
                logger.exception("broker connection failed")
            down.bad(f"connection failed: {exc}")
        logger.debug("broker: retry in %.0f s", delay)
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except TimeoutError:
            pass
        delay = min(delay * 2, BACKOFF_MAX_S)
