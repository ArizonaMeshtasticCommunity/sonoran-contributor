"""Configuration, from environment variables only.

A compose block is the whole setup (design 3.4). Every variable is in
the README. A setting that is wrong stops the agent at start, with every
problem named at once, because a station that runs on a guess sends
data nobody can trust. Bad data from a decoder, later, never stops it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from rf_agent.messages import AIRCRAFT_KINDS, STATION_ID

logger = logging.getLogger(__name__)

ALL_KINDS = ("adsb", "uat", "aprs", "sonde", "ism")
BEAST_PORT = 30005
DUMP978_JSON_PORT = 30979
MQTTS_PORT = 8883


class ConfigError(ValueError):
    """One or more settings are missing or wrong."""


@dataclass(frozen=True)
class Source:
    """One decoder to read.

    `reader` is `beast` (readsb Beast output over TCP), `aircraft_json`
    (readsb or dump1090-fa `aircraft.json`, over HTTP or from a file), or
    `dump978` (dump978-fa JSON lines over TCP).
    """

    kind: str
    reader: str
    url: str
    host: str = ""
    port: int = 0

    @property
    def name(self) -> str:
        return f"{self.kind}={self.url}"


@dataclass(frozen=True)
class Config:
    station_id: str
    broker_host: str
    broker_port: int
    broker_user: str
    broker_pass: str
    sources: tuple[Source, ...]
    station_lat: float | None = None
    station_lon: float | None = None
    precision_m: int = 1000
    adsb_interval_s: float = 20.0
    log_level: str = "INFO"
    ignored_kinds: tuple[str, ...] = field(default=())

    @property
    def station(self) -> tuple[float, float] | None:
        """The antenna's true position. It never leaves the site."""
        if self.station_lat is None or self.station_lon is None:
            return None
        return self.station_lat, self.station_lon

    @property
    def kinds(self) -> list[str]:
        """The kinds this station serves, for the heartbeat."""
        return sorted({s.kind for s in self.sources})


def _source(kind: str, url: str) -> Source:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "file":
        if not parts.path:
            raise ConfigError(f"SOURCES: {kind}={url} names no file")
        return Source(kind, "aircraft_json", url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ConfigError(f"SOURCES: {kind}={url} has a bad port") from exc
    if not parts.hostname:
        raise ConfigError(f"SOURCES: {kind}={url} names no host")
    if scheme == "beast":
        return Source(kind, "beast", url, parts.hostname, port or BEAST_PORT)
    if scheme == "tcp":
        return Source(kind, "dump978", url, parts.hostname, port or DUMP978_JSON_PORT)
    if scheme in ("http", "https"):
        return Source(kind, "aircraft_json", url, parts.hostname, port or 0)
    raise ConfigError(
        f"SOURCES: {kind}={url}: the scheme must be beast://, tcp://, http://, https:// or file://"
    )


def parse_sources(text: str) -> tuple[tuple[Source, ...], tuple[str, ...]]:
    """Parse `kind=url` pairs. Returns the sources and the kinds set aside.

    Kinds of a later phase are accepted and set aside with a warning, so
    a member's compose file does not break when they list one early.
    """
    sources: list[Source] = []
    ignored: list[str] = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        kind, sep, url = item.partition("=")
        kind, url = kind.strip().lower(), url.strip()
        if not sep or not url:
            raise ConfigError(f"SOURCES: {item!r} is not kind=url")
        if kind not in ALL_KINDS:
            raise ConfigError(f"SOURCES: unknown kind {kind!r}; kinds are {', '.join(ALL_KINDS)}")
        if kind not in AIRCRAFT_KINDS:
            ignored.append(kind)
            continue
        sources.append(_source(kind, url))
    return tuple(sources), tuple(ignored)


def _float(env: Mapping[str, str], name: str, errors: list[str]) -> float | None:
    text = env.get(name, "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        errors.append(f"{name} must be a number, not {text!r}")
        return None
    if not math.isfinite(value):
        errors.append(f"{name} must be a finite number")
        return None
    return value


def load(env: Mapping[str, str]) -> Config:
    """Read the configuration. Raises ConfigError naming every problem."""
    errors: list[str] = []

    station_id = env.get("STATION_ID", "").strip()
    if not STATION_ID.match(station_id):
        errors.append("STATION_ID must match [a-z0-9-]{3,32}")

    broker_host, broker_port = "", MQTTS_PORT
    broker_url = env.get("BROKER_URL", "").strip()
    parts = urlsplit(broker_url)
    if not broker_url:
        errors.append("BROKER_URL is required, such as mqtts://broker.example:8883")
    elif parts.scheme.lower() != "mqtts":
        # The broker is reached over the internet. TLS is not optional.
        errors.append("BROKER_URL must start with mqtts://; the agent only talks TLS to a broker")
    elif not parts.hostname:
        errors.append("BROKER_URL names no host")
    else:
        broker_host = parts.hostname
        try:
            broker_port = parts.port or MQTTS_PORT
        except ValueError:
            errors.append("BROKER_URL has a bad port")

    broker_user = env.get("BROKER_USER", "").strip() or station_id
    broker_pass = env.get("BROKER_PASS", "")

    sources: tuple[Source, ...] = ()
    ignored: tuple[str, ...] = ()
    try:
        sources, ignored = parse_sources(env.get("SOURCES", ""))
    except ConfigError as exc:
        errors.append(str(exc))
    else:
        if not sources:
            errors.append("SOURCES names no adsb or uat decoder, such as adsb=beast://readsb:30005")

    lat = _float(env, "STATION_LAT", errors)
    lon = _float(env, "STATION_LON", errors)
    if (lat is None) != (lon is None):
        errors.append("STATION_LAT and STATION_LON are set together or not at all")
    if lat is not None and not -90 <= lat <= 90:
        errors.append("STATION_LAT must be between -90 and 90")
    if lon is not None and not -180 <= lon <= 180:
        errors.append("STATION_LON must be between -180 and 180")

    precision_text = env.get("STATION_PRECISION_M", "").strip() or "1000"
    precision = 1000
    try:
        precision = int(precision_text)
        if not 0 <= precision <= 100_000:
            errors.append("STATION_PRECISION_M must be between 0 and 100000")
    except ValueError:
        errors.append(f"STATION_PRECISION_M must be a whole number, not {precision_text!r}")

    interval = _float(env, "ADSB_INTERVAL_S", errors)
    if interval is None:
        interval = 20.0
    elif interval < 1:
        errors.append("ADSB_INTERVAL_S must be at least 1")

    log_level = env.get("LOG_LEVEL", "").strip().upper() or "INFO"
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")

    if errors:
        raise ConfigError("; ".join(errors))
    return Config(
        station_id=station_id,
        broker_host=broker_host,
        broker_port=broker_port,
        broker_user=broker_user,
        broker_pass=broker_pass,
        sources=sources,
        station_lat=lat,
        station_lon=lon,
        precision_m=precision,
        adsb_interval_s=interval,
        log_level=log_level,
        ignored_kinds=ignored,
    )
