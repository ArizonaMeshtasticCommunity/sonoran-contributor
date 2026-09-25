"""From decoder output to broker messages, end to end, checked by tools/validate.py."""

import json

from rf_agent.agent import Pipeline, handler_for
from rf_agent.config import load
from rf_agent.publisher import Publisher

from conftest import EPOCH, FIXTURES, SITE, replay_beast, validate

CONFIG = load(
    {
        "STATION_ID": "test-site",
        "BROKER_URL": "mqtts://broker.example",
        "SOURCES": (
            "adsb=beast://readsb,adsb=http://readsb/data/aircraft.json,"
            "uat=tcp://dump978,spectrum=tcp://rf-sweep,"
            "aprs=kiss://direwolf,sonde=udp://0.0.0.0,ism=udp://0.0.0.0:10433"
        ),
        "ISM_ALLOW": "Acurite-Tower",
        "SITE_MESH_NODE": "!a1b2c3d4",
        "STATION_LAT": str(SITE[0]),
        "STATION_LON": str(SITE[1]),
    }
)


class Recorder:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))


async def test_every_message_passes_the_validator(
    tmp_path, beast_capture, aircraft_snapshots, dump978_capture
):
    pipeline = Pipeline(CONFIG)
    handlers = [handler_for(s, CONFIG, pipeline) for s in CONFIG.sources]
    beast, snapshots, uat, sweep, aprs, sonde, ism = handlers
    assert beast.__self__.station == SITE  # the true position, used locally

    tracker = beast.__self__
    pipeline.accept(list(replay_beast(tracker, beast_capture)))
    for n, snapshot in enumerate(aircraft_snapshots):
        pipeline.accept(snapshots(snapshot, EPOCH + n))
    pipeline.accept(uat(dump978_capture, EPOCH))
    sweep((FIXTURES / "rtl_power.csv").read_bytes(), EPOCH + 30)
    pipeline.accept(aprs((FIXTURES / "aprs.kiss").read_bytes(), EPOCH))
    for n, line in enumerate((FIXTURES / "sonde.jsonl").read_bytes().splitlines()):
        pipeline.accept(sonde(line, EPOCH + n))
    for line in (FIXTURES / "rtl433.jsonl").read_bytes().splitlines():
        pipeline.accept(ism(line, EPOCH))
    pipeline.expire(EPOCH + 10_000)

    client = Recorder()
    publisher = Publisher(CONFIG, pipeline.outbox, pipeline.counts, clock=lambda: EPOCH + 300)
    sent = await publisher.flush(client)
    counts = await publisher.heartbeat(client)

    assert counts.heard == sent + counts.dropped
    assert counts.sent == sent > 0
    paths = []
    for n, (topic, payload) in enumerate(client.published):
        message = json.loads(payload)
        assert topic.startswith("rf/v1/test-site/")
        assert topic.rsplit("/", 1)[1] == message.get("kind", "status")
        path = tmp_path / f"{n:03d}.json"
        path.write_bytes(payload)
        paths.append(str(path))
    assert validate.main(paths) == 0
    topics = [topic for topic, _ in client.published]
    assert topics.count("rf/v1/test-site/spectrum") == 1
    # Every kind this station serves reached its own topic.
    assert {t.rsplit("/", 1)[1] for t in topics} == {
        "adsb", "uat", "spectrum", "aprs", "sonde", "ism", "status",
    }  # fmt: skip
    assert json.loads(client.published[-1][1])["mesh_node"] == "!a1b2c3d4"
