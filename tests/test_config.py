"""Configuration from the environment."""

import subprocess
import sys

import pytest

from rf_agent.config import ConfigError, load

BASE = {
    "STATION_ID": "test-site",
    "BROKER_URL": "mqtts://broker.example:8883",
    "BROKER_PASS": "secret",
    "SOURCES": "adsb=beast://readsb:30005",
}


def env(**changes):
    out = dict(BASE)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def test_defaults():
    config = load(BASE)
    assert config.broker_host == "broker.example" and config.broker_port == 8883
    assert config.broker_user == "test-site"  # the login is the station id
    assert config.precision_m == 1000
    assert config.adsb_interval_s == 20
    assert config.station is None
    assert config.kinds == ["adsb"]
    (source,) = config.sources
    assert (source.reader, source.host, source.port) == ("beast", "readsb", 30005)


def test_the_readme_example():
    config = load(
        {
            "STATION_ID": "tempe-roof",
            "BROKER_URL": "mqtts://broker.example:8883",
            "BROKER_USER": "tempe-roof",
            "BROKER_PASS": "x",
            "SOURCES": "adsb=beast://readsb:30005,uat=tcp://dump978:30979",
            "STATION_LAT": "33.42",
            "STATION_LON": "-111.94",
            "STATION_PRECISION_M": "1000",
        }
    )
    assert [s.reader for s in config.sources] == ["beast", "dump978"]
    assert config.kinds == ["adsb", "uat"]
    assert config.station == (33.42, -111.94)


def test_every_source_form():
    config = load(
        env(
            SOURCES=(
                " adsb = beast://readsb , adsb=http://readsb:8080/data/aircraft.json,"
                "uat=tcp://dump978, uat=file:///run/skyaware978/aircraft.json,"
            )
        )
    )
    got = [(s.kind, s.reader, s.port) for s in config.sources]
    assert got == [
        ("adsb", "beast", 30005),
        ("adsb", "aircraft_json", 8080),
        ("uat", "dump978", 30979),
        ("uat", "aircraft_json", 0),
    ]


def test_kinds_of_a_later_phase_are_set_aside():
    config = load(env(SOURCES="adsb=beast://readsb,aprs=tcp://direwolf:8001"))
    assert config.ignored_kinds == ("aprs",)
    assert config.kinds == ["adsb"]


@pytest.mark.parametrize(
    "changes,needle",
    [
        ({"STATION_ID": "No_Caps"}, "STATION_ID"),
        ({"STATION_ID": None}, "STATION_ID"),
        ({"BROKER_URL": "mqtt://broker.example:1883"}, "mqtts://"),
        ({"BROKER_URL": None}, "BROKER_URL"),
        ({"BROKER_URL": "mqtts://:8883"}, "no host"),
        ({"SOURCES": ""}, "SOURCES"),
        ({"SOURCES": "aprs=tcp://direwolf:8001"}, "SOURCES"),
        ({"SOURCES": "adsb"}, "kind=url"),
        ({"SOURCES": "boats=tcp://x:1"}, "unknown kind"),
        ({"SOURCES": "adsb=ftp://x"}, "scheme"),
        ({"SOURCES": "adsb=beast://readsb:notaport"}, "port"),
        ({"STATION_LAT": "33.4"}, "together"),
        ({"STATION_LAT": "95", "STATION_LON": "1"}, "STATION_LAT"),
        ({"STATION_LAT": "north", "STATION_LON": "1"}, "number"),
        ({"STATION_PRECISION_M": "-1"}, "STATION_PRECISION_M"),
        ({"STATION_PRECISION_M": "1.5"}, "whole number"),
        ({"ADSB_INTERVAL_S": "0.5"}, "ADSB_INTERVAL_S"),
        ({"ADSB_INTERVAL_S": "nan"}, "ADSB_INTERVAL_S"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
    ],
)
def test_bad_settings_are_named(changes, needle):
    with pytest.raises(ConfigError, match=needle):
        load(env(**changes))


def test_every_problem_is_named_at_once():
    with pytest.raises(ConfigError) as err:
        load({"BROKER_URL": "mqtt://x"})
    text = str(err.value)
    assert "STATION_ID" in text and "mqtts://" in text and "SOURCES" in text


def test_the_program_stops_on_bad_settings():
    result = subprocess.run(
        [sys.executable, "-m", "rf_agent"],
        env={"PATH": "", "STATION_ID": "x"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "STATION_ID" in result.stderr
