"""Configuration, from environment variables only.

A compose block is the whole setup (design 3.4). Every variable is in
the README. A setting that is wrong stops the agent at start, with every
problem named at once, because a station that runs on a guess sends
data nobody can trust. Bad data from a decoder, later, never stops it.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from rf_agent.ism import parse_allow
from rf_agent.messages import STATION_ID

logger = logging.getLogger(__name__)

ALL_KINDS = ("adsb", "uat", "aprs", "sonde", "ism", "spectrum")
# Everything is built: aircraft (phase 1), the 900 MHz band (phase 2),
# APRS (phase 3), radiosondes and weather sensors (phase 4).
BUILT_KINDS = ALL_KINDS
# Kinds a decoder only broadcasts to, so the agent receives datagrams
# rather than connecting out. See `sources.read_datagrams`.
UDP_KINDS = ("sonde", "ism")
BEAST_PORT = 30005
DUMP978_JSON_PORT = 30979
RF_SWEEP_PORT = 30950
KISS_PORT = 8001
AUTO_RX_PORT = 55672
MQTTS_PORT = 8883


MESH_NODE = re.compile(r"^![0-9a-f]{8}$")


class ConfigError(ValueError):
    """One or more settings are missing or wrong."""


@dataclass(frozen=True)
class Source:
    """One decoder to read.

    `reader` is `beast` (readsb Beast output over TCP), `aircraft_json`
    (readsb or dump1090-fa `aircraft.json`, over HTTP or from a file),
    `dump978` (dump978-fa JSON lines over TCP), `rtl_power` (the sweep
    lines `rf-sweep` serves over TCP), `kiss` (direwolf's KISS stream
    over TCP), `auto_rx` (radiosonde_auto_rx summaries over UDP) or
    `rtl_433` (rtl_433 JSON over UDP).
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
    mesh_node: str | None = None
    # `rtl_433` models a member has named. Empty means none pass.
    ism_allow: frozenset[str] = frozenset()

    @property
    def station(self) -> tuple[float, float] | None:
        """The antenna's true position. It never leaves the site."""
        if self.station_lat is None or self.station_lon is None:
            return None
        return self.station_lat, self.station_lon

    @property
    def kinds(self) -> list[str]:
        """The observation kinds this station serves, for the heartbeat.

        Spectrum is not one: the contract's `kinds` lists what a station
        observes, and its minutes say for themselves that it sweeps.
        """
        return sorted({s.kind for s in self.sources if s.kind != "spectrum"})


def _source(kind: str, url: str) -> Source:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if kind == "aprs":
        if scheme not in ("kiss", "tcp") or not parts.hostname:
            raise ConfigError(
                f"SOURCES: {kind}={url}: APRS is read from direwolf, kiss://host:port"
            )
        try:
            return Source(kind, "kiss", url, parts.hostname, parts.port or KISS_PORT)
        except ValueError as exc:
            raise ConfigError(f"SOURCES: {kind}={url} has a bad port") from exc
    if kind in UDP_KINDS:
        reader = "auto_rx" if kind == "sonde" else "rtl_433"
        default_port = AUTO_RX_PORT if kind == "sonde" else 0
        if scheme != "udp":
            raise ConfigError(
                f"SOURCES: {kind}={url}: this decoder only broadcasts, so it is read "
                f"with udp://host:port, such as {kind}=udp://0.0.0.0:{default_port or 10433}"
            )
        try:
            port = parts.port or default_port
        except ValueError as exc:
            raise ConfigError(f"SOURCES: {kind}={url} has a bad port") from exc
        if not port:
            raise ConfigError(f"SOURCES: {kind}={url} names no port to receive on")
        # The host is which address to receive on, not who to call.
        return Source(kind, reader, url, parts.hostname or "0.0.0.0", port)
    if kind == "spectrum":
        if scheme != "tcp" or not parts.hostname:
            raise ConfigError(
                f"SOURCES: {kind}={url}: spectrum is read from rf-sweep, tcp://host:port"
            )
        try:
            return Source(kind, "rtl_power", url, parts.hostname, parts.port or RF_SWEEP_PORT)
        except ValueError as exc:
            raise ConfigError(f"SOURCES: {kind}={url} has a bad port") from exc
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
        if kind not in BUILT_KINDS:
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
            errors.append(
                "SOURCES names nothing to read, such as adsb=beast://readsb:30005 "
                "or spectrum=tcp://rf-sweep:30950"
            )

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

    mesh_node = None
    node_text = env.get("SITE_MESH_NODE", "").strip().lower()
    if node_text:
        mesh_node = node_text if node_text.startswith("!") else "!" + node_text
        if not MESH_NODE.match(mesh_node):
            errors.append("SITE_MESH_NODE must be a node ID such as !a1b2c3d4")

    ism_allow = parse_allow(env.get("ISM_ALLOW", ""))
    if any(s.kind == "ism" for s in sources):
        if not ism_allow:
            errors.append(
                "ISM_ALLOW is empty, so no rtl_433 device would pass; name the models to send"
            )
        if lat is None or lon is None:
            errors.append(
                "a weather sensor sends no position, so STATION_LAT and STATION_LON are "
                "needed to place what ism hears"
            )

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
        mesh_node=mesh_node,
        ism_allow=ism_allow,
    )
