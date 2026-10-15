"""The member's own Meshtastic node, through the Meshtastic Python library.

The library is optional (`pip install sonoran-contributor[mesh]`), so an
RF-only station does not carry it. It talks to the node over its TCP
API or a serial port, from its own threads; this module wraps the few
calls a job needs in `asyncio.to_thread`.

It sends one kind of packet: a TELEMETRY_APP request for LocalStats to
one node. Nothing here sends an admin message or changes a setting.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from sonoran_contributor.jobs import LinkError, Reply

logger = logging.getLogger(__name__)

# The library's own default, DEFAULT_TCP_PORT in meshtastic/tcp_interface.py
# (meshtastic/python 2.7.11).
DEFAULT_TCP_PORT = 4403


def reply_from_packet(packet: dict[str, Any], now: datetime) -> Reply:
    """The LocalStats answer in one packet dict from the library.

    The library puts the decoded protobuf in `decoded.telemetry.raw`
    and the whole MeshPacket in `raw` (meshtastic/mesh_interface.py
    lines 1571 and 1637, 2.7.11). A routing packet instead is a
    negative answer, such as NO_RESPONSE: it raises `LinkError`.
    """
    decoded = packet.get("decoded") or {}
    telemetry = decoded.get("telemetry")
    if telemetry is None:
        routing = decoded.get("routing") or {}
        raise LinkError(str(routing.get("errorReason") or "the answer held no telemetry"))
    raw = telemetry.get("raw")
    if raw is None or not raw.HasField("local_stats"):
        raise LinkError("the answer held no LocalStats")
    stats = raw.local_stats
    values = {field.name: getattr(stats, field.name) for field in stats.DESCRIPTOR.fields}
    # The firmware sets noise_floor only when the radio can measure it,
    # and leaves it 0 otherwise (getLocalStatsTelemetry in
    # src/modules/Telemetry/DeviceTelemetry.cpp, meshtastic/firmware
    # v2.7.26.54e0d8d). 0 dBm is "not measured", never a reading.
    if values.get("noise_floor") == 0:
        values.pop("noise_floor")
    hops = rx_snr = rx_rssi = None
    mesh_packet = packet.get("raw")
    if mesh_packet is not None:
        if mesh_packet.hop_start:
            hops = max(0, mesh_packet.hop_start - mesh_packet.hop_limit)
        rx_snr = mesh_packet.rx_snr or None
        rx_rssi = mesh_packet.rx_rssi or None
    return Reply(values, now, hops=hops, rx_snr=rx_snr, rx_rssi=rx_rssi)


class MeshtasticLink:
    """One open connection to the member's node."""

    def __init__(self, host: str | None, port: int, serial: str | None) -> None:
        self.host = host
        self.port = port
        self.serial = serial
        self.interface: Any = None
        # One request at a time: a contributor has one job at a time, and
        # the library's response handlers are keyed by packet id only.
        self._lock = asyncio.Lock()

    def _open(self) -> Any:
        if self.serial:
            from meshtastic.serial_interface import SerialInterface

            return SerialInterface(devPath=self.serial)
        from meshtastic.tcp_interface import TCPInterface

        return TCPInterface(hostname=self.host, portNumber=self.port)

    async def connect(self) -> None:
        if self.interface is None:
            self.interface = await asyncio.to_thread(self._open)

    async def close(self) -> None:
        interface, self.interface = self.interface, None
        if interface is not None:
            await asyncio.to_thread(interface.close)

    def my_node(self) -> int | None:
        info = getattr(self.interface, "myInfo", None)
        return getattr(info, "my_node_num", None) if info is not None else None

    def nodes(self) -> list[tuple[int, int | None, float | None]]:
        """(node, hops away, last heard epoch) for each node in the node list."""
        known = getattr(self.interface, "nodesByNum", None) or {}
        return [
            (int(num), info.get("hopsAway"), info.get("lastHeard"))
            for num, info in list(known.items())
        ]

    async def request_local_stats(self, target: int, wait_s: float) -> Reply | None:
        from meshtastic.protobuf import portnums_pb2, telemetry_pb2

        async with self._lock:
            if self.interface is None:
                raise LinkError("not connected to the node")
            answered = threading.Event()
            box: dict[str, Any] = {}

            def on_response(packet: dict[str, Any]) -> None:
                box["packet"] = packet
                answered.set()

            request = telemetry_pb2.Telemetry()
            request.local_stats.CopyFrom(telemetry_pb2.LocalStats())
            await asyncio.to_thread(
                self.interface.sendData,
                request,
                destinationId=target,
                portNum=portnums_pb2.PortNum.TELEMETRY_APP,
                wantResponse=True,
                onResponse=on_response,
            )
            if not await asyncio.to_thread(answered.wait, wait_s):
                return None
            return reply_from_packet(box["packet"], datetime.now(UTC))
