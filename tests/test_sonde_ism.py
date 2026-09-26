"""Radiosondes and weather sensors: the readers, and the allow-list."""

import json

import pytest

from rf_agent.ism import Rtl433, parse_allow, thing_id
from rf_agent.messages import batch
from rf_agent.sonde import AutoRx

from conftest import EPOCH, FIXTURES, SITE, assert_valid

ALLOW = parse_allow("Acurite-Tower, fineoffset-wh51")
ROUNDED = (32.79905, -113.29948)


def datagrams(name: str) -> list[bytes]:
    return (FIXTURES / name).read_bytes().splitlines()


def test_a_sonde_flight_is_read():
    reader = AutoRx()
    points = []
    for n, line in enumerate(datagrams("sonde.jsonl")):
        points += reader.feed(line, EPOCH + n)
    assert len(points) == 50
    assert {p.id for p in points} == {"s4132077"}
    first = points[0]
    assert first.alt_m == 1200 and first.alt_ref == "geom"
    # 46.3 km/h is 25 knots.
    assert first.gs_kn == pytest.approx(25.0, abs=0.1)
    assert first.track_deg == 70
    assert first.meta["model"] == "RS41" and first.meta["sats"] == 9
    # An SNR is not a level, so it does not become rssi_db.
    assert first.rssi_db is None and first.meta["snr"] == 14.2
    # Station announcements are not payloads, and one line is malformed.
    assert reader.ignored == 3
    assert reader.bad == 1
    for start in range(0, len(points), 100):
        assert_valid(batch("test-site", "sonde", points[start : start + 100], EPOCH))


def test_sonde_bad_input_is_counted():
    reader = AutoRx()
    assert reader.feed(b"", EPOCH) == []
    assert reader.feed(b"[1,2,3]", EPOCH) == []
    assert reader.feed(b'{"type":"PAYLOAD_SUMMARY","callsign":"x y z"}', EPOCH) == []
    assert reader.bad == 3


def test_only_allow_listed_sensors_are_sent():
    reader = Rtl433("ism", ALLOW, ROUNDED)
    points = []
    for line in datagrams("rtl433.jsonl"):
        points += reader.feed(line, EPOCH)
    assert len(points) == 21
    # The device with no id of its own is named by its channel.
    assert {p.id for p in points} == {"acurite-tower-1234", "acurite-tower-b"}
    # The tyre sensor nobody named never leaves the site.
    assert reader.not_allowed == 4
    assert reader.bad == 1  # the malformed line
    point = points[0]
    assert point.meta["model"] == "Acurite-Tower"
    assert point.meta["temperature_c"] == 21.5 and point.meta["humidity"] == 44
    assert point.rssi_db == -11.2
    # A sensor sends no position: it is drawn where it was heard.
    assert (point.lat, point.lon) == ROUNDED
    for start in range(0, len(points), 100):
        assert_valid(batch("test-site", "ism", points[start : start + 100], EPOCH))


def test_nothing_passes_with_an_empty_allow_list():
    reader = Rtl433("ism", frozenset(), ROUNDED)
    for line in datagrams("rtl433.jsonl"):
        assert reader.feed(line, EPOCH) == []
    assert reader.not_allowed == 25


def test_the_allow_list_ignores_case_but_not_prefixes():
    allow = parse_allow(" Acurite-Tower ,, fineoffset-WH51 ")
    assert allow == {"acurite-tower", "fineoffset-wh51"}
    reader = Rtl433("ism", parse_allow("acurite"), ROUNDED)
    line = json.dumps({"model": "Acurite-Tower", "id": 1, "temperature_C": 1}).encode()
    assert reader.feed(line, EPOCH) == []
    assert reader.not_allowed == 1


def test_the_id_starts_with_the_model_so_sonoran_can_check_it():
    assert thing_id("Acurite-Tower", 1234, None) == "acurite-tower-1234"
    assert thing_id("Fineoffset-WH51", None, "B") == "fineoffset-wh51-b"
    assert thing_id("Weird Model!", 7, None) == "weird-model-7"
    # Nothing to name it by: two sensors must not merge into one thing.
    assert thing_id("Acurite-Tower", None, None) is None


def test_a_sensor_needs_a_station_position():
    reader = Rtl433("ism", ALLOW, None)
    line = json.dumps({"model": "Acurite-Tower", "id": 1, "temperature_C": 1}).encode()
    assert reader.feed(line, EPOCH) == []
    assert reader.bad == 1


def test_syslog_wrapping_is_unwrapped():
    reader = Rtl433("ism", ALLOW, ROUNDED)
    body = json.dumps({"model": "Acurite-Tower", "id": 9, "temperature_C": 3.5})
    (point,) = reader.feed(f"<165>1 2026-09-22T18:00:00Z host rtl433 - - - {body}".encode(), EPOCH)
    assert point.id == "acurite-tower-9"
    assert (point.lat, point.lon) == ROUNDED
    assert SITE != ROUNDED  # the true position never leaves the site


def test_raw_is_the_line_the_decoder_sent():
    line = datagrams("sonde.jsonl")[0]
    (point,) = AutoRx().feed(line, EPOCH)
    assert point.raw == line.decode()
    reader = Rtl433("ism", ALLOW, ROUNDED)
    body = json.dumps({"model": "Acurite-Tower", "id": 9, "temperature_C": 3.5})
    wrapped = f"<165>1 2026-09-22T18:00:00Z host rtl433 - - - {body}".encode()
    (point,) = reader.feed(wrapped, EPOCH)
    # The device's own line, without the syslog wrapper around it.
    assert point.raw == body
