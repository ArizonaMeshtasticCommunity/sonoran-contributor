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
# The largest hop limit a packet carries: HOP_MAX in src/mesh/MeshTypes.h
# line 38, meshtastic/firmware v2.8.1.8e6a88d. A configured limit above
# it is read as it (Default::getConfiguredOrDefaultHopLimit, Default.cpp
# 92-98).
HOP_MAX = 7


def request_hop_limit(hops_away: int | None, configured: int) -> int:
    """The hop limit for a request to a node `hops_away` hops away.

    The firmware's own rule for the reply to a request
    (RoutingModule::getHopLimitForResponse, src/modules/RoutingModule.cpp
    lines 64-81, v2.8.1.8e6a88d): the hops used plus 2 of margin, when
    that is below the node's limit; the limit otherwise; the hops used
    when they are more than the limit; the limit when they are unknown.
    Sending with the node's own limit instead let a request to a
    neighbour flood the whole mesh at 7 hops (2026-10-17).
    """
    limit = min(configured, HOP_MAX)
    if hops_away is None:
        return limit
    if hops_away > limit:
        return hops_away
    if hops_away + 2 < limit:
        return hops_away + 2
    return limit


def offered_nodes(
    known: dict[int, dict[str, Any]], me: int | None, uses_pki: bool
) -> list[tuple[int, int | None, float | None]]:
    """(node, hops away, last heard) for the nodes this contributor can ask.

    Not our own node: a request to it is answered locally, not over the
    air, and the reply took another path (2026-10-17). With PKI, not a
    node whose public key our node lacks: the firmware refuses that
    direct message (`refuse legacy DM`, src/mesh/Router.cpp lines
    1350-1354, v2.8.1.8e6a88d). A licensed node sends no PKI
    (wouldEncryptWithPKC, Router.cpp line 1198), so it can ask them all.
    """
    out = []
    for num, info in list(known.items()):
        if me is not None and int(num) == me:
            continue
        if uses_pki and not (info.get("user") or {}).get("publicKey"):
            continue
        out.append((int(num), info.get("hopsAway"), info.get("lastHeard")))
    return out


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
        if routing:
            raise LinkError(str(routing.get("errorReason") or "a routing answer with no error"))
        # Neither LocalStats nor a routing error: say what it was.
        raise LinkError(f"an answer without telemetry, portnum {decoded.get('portnum', '?')}")
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


def tee_class(base: type) -> type:
    """`base`, a library interface, that also hands every raw frame from
    the node to `tee` before handling it: the proxy's feed. Set before
    the library connects, so the config frames pass through it too."""

    class Tee(base):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, tee: Any = None, **kwargs: Any) -> None:
            self._tee = tee
            super().__init__(*args, **kwargs)

        def _handleFromRadio(self, fromRadioBytes: bytes) -> None:
            if self._tee is not None:
                self._tee(bytes(fromRadioBytes))
            super()._handleFromRadio(fromRadioBytes)

    return Tee


class MeshtasticLink:
    """One open connection to the member's node."""

    def __init__(self, host: str | None, port: int, serial: str | None, tee: Any = None) -> None:
        self.host = host
        self.port = port
        self.serial = serial
        # Every raw frame from the node, for the proxy. None: no proxy.
        self.tee = tee
        # Called before each new connection, so the proxy reads the config again.
        self.before_connect: Any = None
        self.interface: Any = None
        # One request at a time: a contributor has one job at a time, and
        # the library's response handlers are keyed by packet id only.
        self._lock = asyncio.Lock()

    def _open(self) -> Any:
        if self.serial:
            from meshtastic.serial_interface import SerialInterface

            return tee_class(SerialInterface)(devPath=self.serial, tee=self.tee)
        from meshtastic.tcp_interface import TCPInterface

        return tee_class(TCPInterface)(hostname=self.host, portNumber=self.port, tee=self.tee)

    async def connect(self) -> None:
        if self.interface is None:
            if self.before_connect is not None:
                self.before_connect()
            self.interface = await asyncio.to_thread(self._open)

    def send_to_radio(self, to_radio: Any) -> None:
        """Pass one ToRadio from a proxy client to the node, as it is."""
        interface = self.interface
        if interface is not None:
            interface._sendToRadio(to_radio)

    async def close(self) -> None:
        interface, self.interface = self.interface, None
        if interface is not None:
            await asyncio.to_thread(interface.close)

    def my_node(self) -> int | None:
        info = getattr(self.interface, "myInfo", None)
        return getattr(info, "my_node_num", None) if info is not None else None

    def uses_pki(self) -> bool:
        """True when our node sends direct messages with PKI: it has a key
        and is not licensed (wouldEncryptWithPKC, Router.cpp 1189-1213)."""
        local = getattr(self.interface, "localNode", None)
        config = getattr(local, "localConfig", None)
        if config is None:
            return True
        has_key = len(config.security.public_key) == 32
        owner = getattr(self.interface, "getMyUser", lambda: {})() or {}
        return has_key and not owner.get("isLicensed", False)

    def configured_hop_limit(self) -> int:
        local = getattr(self.interface, "localNode", None)
        config = getattr(local, "localConfig", None)
        return int(config.lora.hop_limit) if config is not None else HOP_MAX

    def nodes(self) -> list[tuple[int, int | None, float | None]]:
        """(node, hops away, last heard epoch) for each node this contributor can ask."""
        known = getattr(self.interface, "nodesByNum", None) or {}
        return offered_nodes(known, self.my_node(), self.uses_pki())

    async def request_local_stats(self, target: int, wait_s: float) -> Reply | None:
        from meshtastic.protobuf import portnums_pb2, telemetry_pb2

        async with self._lock:
            if self.interface is None:
                # The last one died. Open a new one for this job.
                try:
                    await self.connect()
                except Exception as exc:
                    raise LinkError(f"cannot reach the node: {exc}") from exc
            answered = threading.Event()
            known = getattr(self.interface, "nodesByNum", None) or {}
            hops = (known.get(target) or {}).get("hopsAway")
            box: dict[str, Any] = {}

            def on_response(packet: dict[str, Any]) -> None:
                box["packet"] = packet
                answered.set()

            request = telemetry_pb2.Telemetry()
            request.local_stats.CopyFrom(telemetry_pb2.LocalStats())
            try:
                await asyncio.to_thread(
                    self.interface.sendData,
                    request,
                    destinationId=target,
                    portNum=portnums_pb2.PortNum.TELEMETRY_APP,
                    wantResponse=True,
                    hopLimit=request_hop_limit(hops, self.configured_hop_limit()),
                    onResponse=on_response,
                )
            except Exception as exc:
                # A dead socket or serial port (OSError in the library's
                # _writeBytes). Drop this interface; the next job opens a
                # new one.
                await self.close()
                raise LinkError(f"the node went away: {exc}") from exc
            if not await asyncio.to_thread(answered.wait, wait_s):
                return None
            return reply_from_packet(box["packet"], datetime.now(UTC))
