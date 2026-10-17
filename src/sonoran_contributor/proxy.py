"""An opt-in proxy: other apps reach the member's node through the contributor.

A Meshtastic node serves one TCP API client, and drops the old one when
a new one connects ("Force close previous TCP connection", ServerAPI.cpp
lines 126-140, firmware v2.8.1.8e6a88d). With mesh jobs on, the
contributor holds that connection, so a phone app on WiFi and the
contributor knock each other off. With `MESH_PROXY_PORT` set, apps
connect to the contributor instead:

    phone app ─┐
    web client ├──► contributor :MESH_PROXY_PORT ──► node (one connection)
    CLI       ─┘

- A client's config request is answered from a cache of the node's own
  config and node list, with the client's config id.
- Every frame the node sends after its config goes to every client.
- A client's packets go to the node, except ADMIN_APP: no client may
  change the node's settings through the contributor.
- Heartbeats and config requests are answered here, not sent on.

The port has no authentication. It listens on 127.0.0.1 unless
`MESH_PROXY_BIND` names another address. The design follows MeshMonitor's
Virtual Node Server (docs/configuration/virtual-node.md); no code is
taken from it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from meshtastic.protobuf import mesh_pb2, portnums_pb2

logger = logging.getLogger(__name__)

# The stream API's framing: START1, START2, a 2-byte big-endian length,
# then a FromRadio or ToRadio. START1, START2, HEADER_LEN and
# MAX_TO_FROM_RADIO_SIZE in meshtastic/stream_interface.py (2.7.11).
START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4
MAX_FRAME = 512
# A client that does not read is dropped once this many frames wait for
# it, so one stuck phone cannot hold the others back. A memory cap, not
# a measure.
CLIENT_QUEUE = 256

# What a client may never send to the node.
BLOCKED_PORTNUMS = frozenset({portnums_pb2.PortNum.ADMIN_APP})


def encode_frame(payload: bytes) -> bytes:
    return bytes([START1, START2, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


class FrameReader:
    """Split a byte stream into frame payloads. Junk between frames is skipped."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while True:
            start = self._buf.find(bytes([START1, START2]))
            if start < 0:
                # Keep a trailing START1: the START2 may be in the next read.
                keep = 1 if self._buf[-1:] == bytes([START1]) else 0
                del self._buf[: len(self._buf) - keep]
                return out
            del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                return out
            length = (self._buf[2] << 8) | self._buf[3]
            if length > MAX_FRAME:
                # Not a frame: drop the two start bytes and look again.
                del self._buf[:2]
                continue
            if len(self._buf) < HEADER_LEN + length:
                return out
            out.append(bytes(self._buf[HEADER_LEN : HEADER_LEN + length]))
            del self._buf[: HEADER_LEN + length]


class ProxyHub:
    """The node's config cache, the clients, and what passes between them.

    `on_from_radio` takes every frame the node sends. `handle_client`
    takes one frame from a client and returns the frames to send back to
    that client alone.
    """

    def __init__(self, send_upstream: Callable[[mesh_pb2.ToRadio], None]) -> None:
        self.send_upstream = send_upstream
        self._sinks: list[Callable[[bytes], None]] = []
        self.reset()

    def reset(self) -> None:
        """A new connection to the node: its config is read again."""
        self.ready = False
        self._my_info: bytes | None = None
        self._nodes: dict[int, bytes] = {}
        self._rest: list[bytes] = []

    def subscribe(self, sink: Callable[[bytes], None]) -> None:
        self._sinks.append(sink)

    def unsubscribe(self, sink: Callable[[bytes], None]) -> None:
        with contextlib.suppress(ValueError):
            self._sinks.remove(sink)

    def on_from_radio(self, raw: bytes) -> None:
        msg = mesh_pb2.FromRadio()
        try:
            msg.ParseFromString(raw)
        except Exception:
            return
        kind = msg.WhichOneof("payload_variant")
        if kind == "my_info" and self.ready:
            # The library reconnected and reads the config again.
            self.reset()
        if kind == "node_info":
            self._nodes[msg.node_info.num] = raw
        if not self.ready:
            if kind == "my_info":
                self._my_info = raw
            elif kind == "config_complete_id":
                self.ready = True
            elif kind != "node_info":
                self._rest.append(raw)
            return
        for sink in list(self._sinks):
            sink(raw)

    def config_frames(self, config_id: int) -> list[bytes]:
        frames = [self._my_info] if self._my_info else []
        frames += list(self._nodes.values())
        frames += self._rest
        frames.append(mesh_pb2.FromRadio(config_complete_id=config_id).SerializeToString())
        return frames

    def handle_client(self, raw: bytes) -> list[bytes]:
        msg = mesh_pb2.ToRadio()
        try:
            msg.ParseFromString(raw)
        except Exception:
            return []
        kind = msg.WhichOneof("payload_variant")
        if kind == "want_config_id":
            return self.config_frames(msg.want_config_id) if self.ready else []
        if kind == "packet":
            portnum = msg.packet.decoded.portnum if msg.packet.HasField("decoded") else None
            if portnum in BLOCKED_PORTNUMS:
                logger.info("proxy: dropped a client's ADMIN_APP packet")
                return []
            self.send_upstream(msg)
        # Heartbeats, disconnects and the rest stay here.
        return []


async def serve(hub: ProxyHub, bind: str, port: int) -> asyncio.Server:
    """Listen for apps on `bind`:`port` and serve them from `hub`."""

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue(CLIENT_QUEUE)
        loop = asyncio.get_running_loop()

        def sink(raw: bytes) -> None:
            if queue.full():
                writer.close()
            else:
                queue.put_nowait(raw)

        async def pump() -> None:
            while True:
                raw = await queue.get()
                writer.write(encode_frame(raw))
                await writer.drain()

        hub.subscribe(sink)
        sender = loop.create_task(pump())
        frames = FrameReader()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for payload in frames.feed(data):
                    for reply in hub.handle_client(payload):
                        await queue.put(reply)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            hub.unsubscribe(sink)
            sender.cancel()
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(client, bind, port)
    logger.info("node proxy listening on %s:%d", bind, port)
    return server
