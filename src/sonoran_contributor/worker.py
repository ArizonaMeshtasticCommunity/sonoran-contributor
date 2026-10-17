"""Mesh jobs over MQTT: take jobs from our outbox, answer in our inbox.

    outbox/<job> (retained) --> Seen --> queue --> do_job --> inbox/<job>
    every HEARTBEAT_S: status (retained), with the node's heard list

One job at a time. A retained job arrives again after a reconnect; it
is done once. A job that expired while we were away is ignored. If the
broker goes away mid-job, the result is lost and Sonoran gives the job
to someone else when it expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from datetime import UTC, datetime

import aiomqtt

from rf_agent.publisher import BACKOFF_MAX_S, HEARTBEAT_S
from rf_agent.quiet import Condition
from sonoran_contributor.config import MeshConfig
from sonoran_contributor.contract import (
    Job,
    inbox_topic,
    node_id,
    outbox_filter,
    parse_job,
    status_message,
    status_topic,
)
from sonoran_contributor.jobs import Seen, do_job, heard_list
from sonoran_contributor.node import MeshtasticLink
from sonoran_contributor.proxy import ProxyHub, serve

logger = logging.getLogger(__name__)


def _version() -> str:
    from rf_agent import __version__

    return __version__


def connected_line(link: MeshtasticLink) -> str:
    """The log line at each connect, so a member sees which node is used."""
    me = link.my_node()
    name = node_id(me) if me is not None else "(number not known yet)"
    return f"mesh jobs: connected to node {name}, {len(link.nodes())} nodes in its list"


def _client(config: MeshConfig) -> aiomqtt.Client:
    return aiomqtt.Client(
        hostname=config.broker_host,
        port=config.broker_port,
        username=config.broker_user or None,
        password=config.broker_pass or None,
        # Not the RF agent's client id: both may run under one login.
        identifier=f"contributor-{config.broker_user}",
        protocol=aiomqtt.ProtocolVersion.V311,
        clean_session=True,
        keepalive=60,
        tls_context=ssl.create_default_context(),
    )


async def _heartbeat(client: aiomqtt.Client, config: MeshConfig, link: MeshtasticLink) -> None:
    while True:
        now = datetime.now(UTC)
        message = status_message(
            config.location,
            config.source,
            now,
            _version(),
            link.my_node(),
            config.allow,
            heard_list(link.nodes(), now),
        )
        await client.publish(
            status_topic(config.location, config.source),
            json.dumps(message, separators=(",", ":")),
            qos=1,
            retain=True,
        )
        await asyncio.sleep(HEARTBEAT_S)


async def _jobs(
    client: aiomqtt.Client,
    config: MeshConfig,
    link: MeshtasticLink,
    queue: asyncio.Queue[Job],
) -> None:
    while True:
        job = await queue.get()
        result = await do_job(job, link, config.allow, config.location, config.source)
        if result is None:
            logger.info("job %s expired before it started; ignored", job.job)
            continue
        await client.publish(
            inbox_topic(config.location, config.source, job.job),
            json.dumps(result, separators=(",", ":")),
            qos=1,
        )
        logger.info("job %s for %s: %s", job.job, job.target, result["outcome"])


async def _start_proxy(config: MeshConfig, link: MeshtasticLink) -> asyncio.Server | None:
    """The opt-in node proxy (proxy.py), when MESH_PROXY_PORT is set."""
    if config.proxy_port is None:
        return None
    loop = asyncio.get_running_loop()

    def upstream(to_radio: object) -> None:
        # Off the event loop: the library writes to the socket.
        loop.run_in_executor(None, link.send_to_radio, to_radio)

    hub = ProxyHub(send_upstream=upstream)
    # The library reads frames in its own thread; the hub lives on the loop.
    link.tee = lambda raw: loop.call_soon_threadsafe(hub.on_from_radio, raw)
    link.before_connect = hub.reset
    return await serve(hub, config.proxy_bind, config.proxy_port)


async def run(config: MeshConfig, stop: asyncio.Event) -> None:
    """Run the mesh worker until `stop` is set."""
    link = MeshtasticLink(config.node_host, config.node_port, config.node_serial)
    proxy = await _start_proxy(config, link)
    node_down = Condition(logger, "Meshtastic node")
    broker_down = Condition(logger, "broker")
    seen = Seen()
    delay = 1.0
    while not stop.is_set():
        try:
            fresh = link.interface is None
            await link.connect()
            node_down.ok()
            if fresh:
                logger.info(connected_line(link))
        except Exception as exc:
            node_down.bad(str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
            continue
        try:
            async with _client(config) as client:
                broker_down.ok()
                delay = 1.0
                await client.subscribe(outbox_filter(config.location, config.source), qos=1)
                queue: asyncio.Queue[Job] = asyncio.Queue()
                tasks = [
                    asyncio.create_task(_heartbeat(client, config, link), name="heartbeat"),
                    asyncio.create_task(_jobs(client, config, link, queue), name="jobs"),
                    asyncio.create_task(stop.wait(), name="stop"),
                ]
                reader = asyncio.create_task(_read(client, seen, queue), name="outbox")
                tasks.append(reader)
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.get_name() != "stop" and task.exception() is not None:
                        raise task.exception()  # type: ignore[misc]
        except aiomqtt.MqttError as exc:
            broker_down.bad(str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
    await link.close()
    if proxy is not None:
        proxy.close()
        await proxy.wait_closed()


async def _read(client: aiomqtt.Client, seen: Seen, queue: asyncio.Queue[Job]) -> None:
    async for message in client.messages:
        topic = str(message.topic)
        job_id = topic.rsplit("/", 1)[-1]
        try:
            job = parse_job(bytes(message.payload), job_id)
        except ValueError as exc:
            logger.info("ignore a bad job on %s: %s", topic, str(exc)[:200])
            continue
        if job is not None and seen.first(job.job):
            queue.put_nowait(job)
