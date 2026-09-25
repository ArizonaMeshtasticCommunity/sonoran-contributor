"""Connect to the decoders: outgoing connections only, retried with backoff.

A decoder that restarts, or a network that drops, must not need the
agent restarted (design 3.3). Each source runs in its own loop. It
reads, hands the bytes to its parser, and on any failure waits and
connects again, up to a minute apart. A parser error is logged and the
bytes are skipped; it never ends the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from rf_agent.messages import Observation

logger = logging.getLogger(__name__)

BACKOFF_MAX_S = 60.0
CONNECT_TIMEOUT_S = 10.0
# A stream silent for this long is assumed dead and opened again. At
# night a quiet sky is normal, so this is long.
IDLE_S = 300.0
POLL_S = 1.0
MAX_JSON_BYTES = 16 << 20

Handler = Callable[[bytes, float], list[Observation]]
Sink = Callable[[list[Observation]], None]


class Backoff:
    def __init__(self, first: float = 1.0, most: float = BACKOFF_MAX_S) -> None:
        self.first, self.most = first, most
        self.delay = first

    def reset(self) -> None:
        self.delay = self.first

    def next(self) -> float:
        delay = self.delay
        self.delay = min(self.delay * 2, self.most)
        return delay


# When each source last logged a parse failure. One a minute is enough
# to see a source pointed at the wrong port without flooding the log.
_last_complaint: dict[str, float] = {}


def _handle(name: str, handler: Handler, data: bytes, sink: Sink) -> None:
    try:
        points = handler(data, time.time())
        if points:
            sink(points)
    except Exception as exc:
        now = time.monotonic()
        if now - _last_complaint.get(name, -60.0) >= 60.0:
            _last_complaint[name] = now
            logger.warning("%s: skipped input the parser could not read: %s", name, exc)


async def read_stream(
    name: str,
    host: str,
    port: int,
    handler: Handler,
    sink: Sink,
    idle_s: float = IDLE_S,
    backoff: Backoff | None = None,
) -> None:
    """Read a decoder's TCP output forever."""
    backoff = backoff or Backoff()
    while True:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT_S
            )
            logger.info("%s: connected", name)
            backoff.reset()
            while True:
                data = await asyncio.wait_for(reader.read(65536), idle_s)
                if not data:
                    logger.warning("%s: the decoder closed the connection", name)
                    break
                _handle(name, handler, data, sink)
        except TimeoutError:
            logger.warning("%s: no answer, or nothing heard for a while; reconnecting", name)
        except OSError as exc:
            logger.warning("%s: %s", name, exc)
        except Exception:
            logger.exception("%s: reader failed", name)
        finally:
            if writer is not None:
                writer.close()
        delay = backoff.next()
        logger.info("%s: retry in %.0f s", name, delay)
        await asyncio.sleep(delay)


async def read_datagrams(
    name: str,
    host: str,
    port: int,
    handler: Handler,
    sink: Sink,
    backoff: Backoff | None = None,
) -> None:
    """Receive what a decoder broadcasts over UDP, forever.

    `radiosonde_auto_rx` and `rtl_433` only broadcast; they accept no
    connection. So this is the one place the agent binds a socket, and
    it is a UDP one: no connection is accepted, nothing is served, and
    anything may send to it. Every datagram is treated as untrusted
    input and a bad one is counted and skipped, like a bad frame.
    """
    backoff = backoff or Backoff()
    loop = asyncio.get_running_loop()

    class _Receiver(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: object) -> None:
            _handle(name, handler, data, sink)

        def error_received(self, exc: Exception) -> None:
            logger.warning("%s: %s", name, exc)

    while True:
        transport = None
        try:
            transport, _ = await loop.create_datagram_endpoint(_Receiver, local_addr=(host, port))
            logger.info("%s: listening for datagrams on %s:%d", name, host, port)
            backoff.reset()
            # Nothing to wait for: the protocol is called as datagrams
            # arrive. Sleep until the socket fails or the task is cancelled.
            await asyncio.Event().wait()
        except OSError as exc:
            logger.warning("%s: %s", name, exc)
        except Exception:
            logger.exception("%s: receiver failed", name)
        finally:
            if transport is not None:
                transport.close()
        delay = backoff.next()
        logger.info("%s: retry in %.0f s", name, delay)
        await asyncio.sleep(delay)


def fetch(url: str, timeout: float = 5.0) -> bytes:
    """Read a local `aircraft.json`, over HTTP or from a file."""
    if urlsplit(url).scheme == "file":
        with open(urlsplit(url).path, "rb") as fh:
            return fh.read(MAX_JSON_BYTES)
    request = urllib.request.Request(url, headers={"User-Agent": "rf-agent"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_JSON_BYTES)


async def poll(
    name: str,
    url: str,
    handler: Handler,
    sink: Sink,
    every_s: float = POLL_S,
    backoff: Backoff | None = None,
    fetcher: Callable[[str], bytes] = fetch,
) -> None:
    """Read `aircraft.json` about once a second, forever."""
    backoff = backoff or Backoff()
    failing = False
    while True:
        try:
            data = await asyncio.to_thread(fetcher, url)
        except Exception as exc:
            if not failing:
                logger.warning("%s: %s", name, exc)
            failing = True
            await asyncio.sleep(backoff.next())
            continue
        if failing:
            logger.info("%s: reading again", name)
            failing = False
            backoff.reset()
        _handle(name, handler, data, sink)
        await asyncio.sleep(every_s)
