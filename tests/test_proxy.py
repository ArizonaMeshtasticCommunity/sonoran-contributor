"""The opt-in node proxy: many clients, one connection to the node.

A Meshtastic node accepts one TCP API client and drops the old one when
a new one connects (ServerAPI.cpp 126-140, firmware v2.8.1). With mesh
jobs on, the contributor holds that connection, so a phone app over
WiFi and the contributor knock each other off. The proxy lets the
phone app connect to the contributor instead. It is off by default.
"""

from __future__ import annotations

import asyncio

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2

from sonoran_contributor.proxy import (
    MAX_FRAME,
    FrameReader,
    ProxyHub,
    encode_frame,
    serve,
)


def _from(**fields) -> bytes:
    msg = mesh_pb2.FromRadio(**fields)
    return msg.SerializeToString()


def _to(**fields) -> bytes:
    return mesh_pb2.ToRadio(**fields).SerializeToString()


def _parse_from(frame_payload: bytes) -> mesh_pb2.FromRadio:
    msg = mesh_pb2.FromRadio()
    msg.ParseFromString(frame_payload)
    return msg


def _upstream_config(hub: ProxyHub, upstream_id: int = 7) -> None:
    hub.on_from_radio(_from(my_info=mesh_pb2.MyNodeInfo(my_node_num=0xA2ED97DC)))
    hub.on_from_radio(_from(node_info=mesh_pb2.NodeInfo(num=0xEAC11565, snr=5.0)))
    hub.on_from_radio(_from(node_info=mesh_pb2.NodeInfo(num=0x799726F2)))
    hub.on_from_radio(_from(config_complete_id=upstream_id))


# --- Framing: the stream API's own (stream_interface.py, meshtastic 2.7.11)


def test_a_frame_is_start_bytes_length_and_payload() -> None:
    assert encode_frame(b"abc") == bytes([0x94, 0xC3, 0, 3]) + b"abc"


def test_the_reader_skips_wake_bytes_and_splits_frames() -> None:
    """The library sends 32 START2 bytes to wake the radio before its first frame."""
    reader = FrameReader()
    data = bytes([0xC3] * 32) + encode_frame(b"one") + encode_frame(b"two")
    assert reader.feed(data[:20]) == []
    assert reader.feed(data[20:]) == [b"one", b"two"]


def test_an_oversized_frame_is_dropped_and_the_stream_resyncs() -> None:
    """MAX_TO_FROM_RADIO_SIZE is 512 in the library."""
    assert MAX_FRAME == 512
    reader = FrameReader()
    bad = bytes([0x94, 0xC3, 0x02, 0x01]) + b"x" * 10
    assert reader.feed(bad + encode_frame(b"ok")) == [b"ok"]


# --- The hub -----------------------------------------------------------


def test_a_client_gets_the_cached_config_with_its_own_id() -> None:
    hub = ProxyHub(send_upstream=lambda _: None)
    _upstream_config(hub)
    frames = [_parse_from(f) for f in hub.handle_client(_to(want_config_id=1234))]
    kinds = [f.WhichOneof("payload_variant") for f in frames]
    assert kinds[0] == "my_info"
    assert kinds.count("node_info") == 2
    assert kinds[-1] == "config_complete_id"
    assert frames[-1].config_complete_id == 1234  # the client's id, not the upstream's 7


def test_a_node_update_replaces_its_cached_entry() -> None:
    hub = ProxyHub(send_upstream=lambda _: None)
    _upstream_config(hub)
    hub.on_from_radio(_from(node_info=mesh_pb2.NodeInfo(num=0xEAC11565, snr=9.5)))
    frames = [_parse_from(f) for f in hub.handle_client(_to(want_config_id=1))]
    snrs = [f.node_info.snr for f in frames if f.node_info.num == 0xEAC11565]
    assert snrs == [9.5]


def test_live_packets_reach_every_client() -> None:
    hub = ProxyHub(send_upstream=lambda _: None)
    _upstream_config(hub)
    a: list[bytes] = []
    b: list[bytes] = []
    hub.subscribe(a.append)
    hub.subscribe(b.append)
    live = _from(packet=mesh_pb2.MeshPacket(id=99, to=0xFFFFFFFF))
    hub.on_from_radio(live)
    assert a == [live] and b == [live]


def test_a_client_packet_goes_upstream() -> None:
    sent: list[mesh_pb2.ToRadio] = []
    hub = ProxyHub(send_upstream=sent.append)
    packet = mesh_pb2.MeshPacket(id=5, to=0xEAC11565)
    packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    assert hub.handle_client(_to(packet=packet)) == []
    assert [t.packet.id for t in sent] == [5]


@pytest.mark.parametrize("portnum", [portnums_pb2.PortNum.ADMIN_APP])
def test_an_admin_packet_from_a_client_never_reaches_the_node(portnum: int) -> None:
    """The node's settings are not the proxy clients' to change."""
    sent: list[mesh_pb2.ToRadio] = []
    hub = ProxyHub(send_upstream=sent.append)
    packet = mesh_pb2.MeshPacket(id=6, to=0xA2ED97DC)
    packet.decoded.portnum = portnum
    hub.handle_client(_to(packet=packet))
    assert sent == []


def test_heartbeats_and_config_requests_are_answered_here_not_upstream() -> None:
    sent: list[mesh_pb2.ToRadio] = []
    hub = ProxyHub(send_upstream=sent.append)
    _upstream_config(hub)
    hub.handle_client(_to(heartbeat=mesh_pb2.Heartbeat()))
    hub.handle_client(_to(want_config_id=3))
    assert sent == []


def test_before_the_node_has_given_its_config_a_client_waits() -> None:
    hub = ProxyHub(send_upstream=lambda _: None)
    assert hub.handle_client(_to(want_config_id=1)) == []
    _upstream_config(hub)
    assert hub.ready


def test_a_new_upstream_connection_forgets_the_old_config() -> None:
    hub = ProxyHub(send_upstream=lambda _: None)
    _upstream_config(hub)
    hub.reset()
    assert not hub.ready


# --- The server, over real sockets on 127.0.0.1 -------------------------


async def _read_frames(reader: asyncio.StreamReader, until_kind: str) -> list:
    frames, fr = [], FrameReader()
    while True:
        chunk = await asyncio.wait_for(reader.read(4096), 5)
        assert chunk, "the proxy closed the connection"
        for payload in fr.feed(chunk):
            msg = _parse_from(payload)
            frames.append(msg)
            if msg.WhichOneof("payload_variant") == until_kind:
                return frames


async def test_two_apps_connect_at_once_and_both_get_live_packets() -> None:
    sent: list[mesh_pb2.ToRadio] = []
    hub = ProxyHub(send_upstream=sent.append)
    _upstream_config(hub)
    server = await serve(hub, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        apps = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
        for n, (_, w) in enumerate(apps):
            w.write(bytes([0xC3] * 32) + encode_frame(_to(want_config_id=100 + n)))
            await w.drain()
        for n, (r, _) in enumerate(apps):
            frames = await _read_frames(r, "config_complete_id")
            assert frames[-1].config_complete_id == 100 + n
        hub.on_from_radio(_from(packet=mesh_pb2.MeshPacket(id=42)))
        for r, _ in apps:
            (frame,) = await _read_frames(r, "packet")
            assert frame.packet.id == 42
        packet = mesh_pb2.MeshPacket(id=77, to=0xEAC11565)
        packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        apps[0][1].write(encode_frame(_to(packet=packet)))
        await apps[0][1].drain()
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.02)
        assert [t.packet.id for t in sent] == [77]
        for _, w in apps:
            w.close()
    finally:
        server.close()
        await server.wait_closed()


# --- Settings ------------------------------------------------------------

ENV = {
    "STATION_ID": "phoenix",
    "SOURCE_ID": "pi5",
    "BROKER_URL": "mqtts://broker.example:8883",
    "MESH_NODE": "tcp://meshnode",
}


def test_the_proxy_is_off_unless_a_port_is_set() -> None:
    from sonoran_contributor.config import load_mesh

    assert load_mesh(ENV).proxy_port is None


def test_the_proxy_listens_on_loopback_unless_told_otherwise() -> None:
    from sonoran_contributor.config import load_mesh

    config = load_mesh({**ENV, "MESH_PROXY_PORT": "4404"})
    assert (config.proxy_bind, config.proxy_port) == ("127.0.0.1", 4404)
    config = load_mesh({**ENV, "MESH_PROXY_PORT": "4404", "MESH_PROXY_BIND": "0.0.0.0"})
    assert config.proxy_bind == "0.0.0.0"


@pytest.mark.parametrize("bad", ["0", "70000", "port"])
def test_a_bad_proxy_port_is_refused(bad: str) -> None:
    from sonoran_contributor.config import ConfigError, load_mesh

    with pytest.raises(ConfigError):
        load_mesh({**ENV, "MESH_PROXY_PORT": bad})
