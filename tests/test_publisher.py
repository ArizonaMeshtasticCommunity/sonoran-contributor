"""Publishing: topics, QoS, batch size, the heartbeat, and a broker that goes away."""

import asyncio
import json
from contextlib import asynccontextmanager

import aiomqtt
import pytest

from rf_agent.config import load
from rf_agent.geo import round_position
from rf_agent.messages import Counts, observation
from rf_agent.publisher import Outbox, Publisher, run

from conftest import assert_valid

T = 1_790_100_000.0
CONFIG_ENV = {
    "STATION_ID": "test-site",
    "BROKER_URL": "mqtts://broker.example",
    "SOURCES": "adsb=beast://readsb,uat=tcp://dump978",
    "STATION_LAT": "32.80013",
    "STATION_LON": "-113.2999",
}
CONFIG = load(CONFIG_ENV)


class FakeClient:
    def __init__(self, fail_after=None):
        self.published = []
        self.fail_after = fail_after

    async def publish(self, topic, payload, qos=0, retain=False):
        if self.fail_after is not None and len(self.published) >= self.fail_after:
            raise aiomqtt.MqttError("connection lost")
        self.published.append((topic, json.loads(payload), qos, retain))


def points(n, kind="adsb"):
    return [observation(kind, f"a{i:05x}", T + i, 33.0, -112.0) for i in range(n)]


def make(counts=None, outbox=None):
    return Publisher(CONFIG, outbox or Outbox(), counts or Counts(), clock=lambda: T)


async def test_batches_hold_at_most_100_points_on_the_kind_topic():
    publisher = make()
    for p in points(250) + points(30, "uat"):
        publisher.outbox.add(p)
    client = FakeClient()
    assert await publisher.flush(client) == 280
    sizes = [(topic, len(msg["obs"]), qos, retain) for topic, msg, qos, retain in client.published]
    assert sizes == [
        ("rf/v1/test-site/adsb", 100, 0, False),
        ("rf/v1/test-site/adsb", 100, 0, False),
        ("rf/v1/test-site/adsb", 50, 0, False),
        ("rf/v1/test-site/uat", 30, 0, False),
    ]
    for _, message, _, _ in client.published:
        assert_valid(message)
        assert message["station"] == "test-site"
    assert publisher.counts.sent == 280
    assert len(publisher.outbox) == 0


async def test_heartbeat_is_retained_qos1_with_counts_and_rounded_position():
    counts = Counts(heard=900, sent=60, dropped=840)
    publisher = make(counts)
    client = FakeClient()
    await publisher.heartbeat(client)
    ((topic, message, qos, retain),) = client.published
    assert (topic, qos, retain) == ("rf/v1/test-site/status", 1, True)
    assert_valid(message)
    assert message["counts"] == {"heard": 900, "sent": 60, "dropped": 840}
    assert (message["lat"], message["lon"]) == round_position(32.80013, -113.2999, 1000)
    assert (message["lat"], message["lon"]) != (32.80013, -113.2999)
    assert message["precision_m"] == 1000
    assert message["kinds"] == ["adsb", "uat"]
    # Counts restart after each heartbeat.
    assert (counts.heard, counts.sent, counts.dropped) == (0, 0, 0)


async def test_a_failed_publish_keeps_the_points_and_the_counts():
    counts = Counts(heard=5)
    publisher = make(counts)
    for p in points(150):
        publisher.outbox.add(p)
    client = FakeClient(fail_after=1)
    with pytest.raises(aiomqtt.MqttError):
        await publisher.flush(client)
    assert len(publisher.outbox) == 50  # the first 100 went
    with pytest.raises(aiomqtt.MqttError):
        await publisher.heartbeat(client)
    assert counts.heard == 5 and counts.sent == 100
    retry = FakeClient()
    await publisher.flush(retry)
    sent_ids = [o["id"] for _, m, _, _ in retry.published for o in m["obs"]]
    assert sent_ids == [p.id for p in points(150)[100:]]  # in order, none lost


def test_outbox_drops_oldest_when_full():
    outbox = Outbox(limit=10)
    for p in points(15):
        outbox.add(p)
    assert len(outbox) == 10 and outbox.overflowed == 5
    assert outbox.take("adsb")[0].id == points(15)[5].id


async def test_run_reconnects_and_sends_after_the_broker_returns(monkeypatch):
    monkeypatch.setattr("rf_agent.publisher.FLUSH_S", 0.01)
    publisher = make()
    for p in points(3):
        publisher.outbox.add(p)
    attempts = []
    client = FakeClient()
    stop = asyncio.Event()

    @asynccontextmanager
    async def connect(config):
        attempts.append(config.broker_host)
        if len(attempts) == 1:
            raise aiomqtt.MqttError("refused")
        yield client

    async def stopper():
        while not any(t.endswith("/adsb") for t, *_ in client.published):
            await asyncio.sleep(0.01)
        stop.set()

    both = asyncio.gather(run(publisher, stop, connect, first_delay=0.01), stopper())
    await asyncio.wait_for(both, 5)
    assert len(attempts) == 2
    topics = [t for t, *_ in client.published]
    assert topics[0] == "rf/v1/test-site/adsb"
    assert "rf/v1/test-site/status" in topics
    for _, message, _, _ in client.published:
        assert_valid(message)


async def test_spectrum_minutes_go_out_on_their_topic_and_survive_a_failure():
    spectrum = {
        "v": 1, "station": "test-site", "kind": "spectrum", "t": "2026-09-22T18:00:00Z",
        "f_start_hz": 902_000_000, "bin_hz": 50_000, "sweeps": 12, "gain_db": 32.8,
        "p50": [-500, None], "p95": [-480, None], "peak": [-470, None], "busy": [0, None],
    }  # fmt: skip
    publisher = make()
    publisher.outbox.post("spectrum", spectrum)
    with pytest.raises(aiomqtt.MqttError):
        await publisher.flush(FakeClient(fail_after=0))
    client = FakeClient()
    await publisher.flush(client)
    ((topic, message, qos, retain),) = client.published
    assert (topic, qos, retain) == ("rf/v1/test-site/spectrum", 0, False)
    assert_valid(message)
    assert not publisher.outbox.posted


async def test_heartbeat_names_the_mesh_node():
    config = load({**CONFIG_ENV, "SITE_MESH_NODE": "!a1b2c3d4"})
    publisher = Publisher(config, Outbox(), Counts(), clock=lambda: T)
    client = FakeClient()
    await publisher.heartbeat(client)
    message = client.published[0][1]
    assert_valid(message)
    assert message["mesh_node"] == "!a1b2c3d4"
