"""The version 1 messages: one observation, a batch, and the heartbeat.

The contract is `schema/rf-message-v1.schema.json`, generated from
Sonoran's `sonoran/rf/schema.py`. This module builds messages that pass
it. A reader hands over raw values; `observation()` checks them, and
returns None for a point that is not one, so a bad value never reaches
the broker and never makes a whole batch invalid.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

VERSION = 1
MAX_OBS = 100
# Sonoran caps `meta` at 4 kB, and each value at 256 characters, which is
# the most an AX.25 information field can hold. Over either, it drops the
# whole of `meta` rather than shorten anything, so the agent keeps to the
# same limits: a value that cannot be sent whole is left out, never cut.
MAX_META_BYTES = 4096
MAX_META_VALUE_CHARS = 256
# The contract's cap on the raw source of one observation.
MAX_RAW_CHARS = 1024
# What a thing's identity looks like, per kind. It is the natural
# identity of the thing heard (design 4.2), and Sonoran checks it again:
# an id outside these shapes is not stored, so a bad one is dropped here
# and counted rather than sent.
#
# An aircraft's leading `~` marks an address that is not an ICAO one,
# such as a TIS-B target, the way readsb prints it. An APRS id carries
# its SSID, because that is how APRS names a station.
AIRCRAFT_ID = re.compile(r"^~?[0-9a-f]{6}$")
ID_RULES = {
    "adsb": AIRCRAFT_ID,
    "uat": AIRCRAFT_ID,
    # A callsign with its SSID, or an object or item a station placed,
    # which APRS names globally and `;` keeps apart from any callsign.
    "aprs": re.compile(r"^(;[a-z0-9._+-][a-z0-9_.+-]{0,8}|[a-z0-9]{1,9}(-[a-z0-9]{1,2})?)$"),
    "sonde": re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$"),
    "ism": re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$"),
}
STATION_ID = re.compile(r"^[a-z0-9-]{3,32}$")
# This agent's name within its station, when the station has more than
# one Pi (Sonoran #305). The station is the place on the map; each Pi is
# a source with its own login and its own heartbeat.
SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")
AIRCRAFT_KINDS = ("adsb", "uat")
FEET = 0.3048


@dataclass(frozen=True, slots=True)
class Observation:
    """One thing heard, at one moment. `t` is seconds since the epoch."""

    kind: str
    id: str
    t: float
    lat: float
    lon: float
    alt_m: float | None = None
    alt_ref: str | None = None
    gs_kn: float | None = None
    track_deg: float | None = None
    rssi_db: float | None = None
    # True when the thing reached this receiver with nothing in between.
    # APRS sets it; a relayed packet says where a station is, but nothing
    # about what this receiver hears. None means it is not known.
    direct: bool | None = None
    # The station whose transmission we decoded, when that is not the
    # thing itself: the last digipeater to repeat it, or the gateway of a
    # packet that came from the internet. None when we heard the thing.
    via: str | None = None
    # How far the sender blurred its own position, in metres. APRS lets a
    # station blank digits to say "somewhere around here". None means it
    # said exactly where it is.
    precision_m: float | None = None
    # What the decoder gave us for this point, as it came: a frame, a
    # line, an object. It is what a reader checks a decoded point
    # against, so it is never shortened.
    raw: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Return the observation as the contract spells it."""
        out: dict[str, Any] = {
            "id": self.id,
            "t": timestamp(self.t),
            "lat": self.lat,
            "lon": self.lon,
        }
        for name in (
            "alt_m",
            "alt_ref",
            "gs_kn",
            "track_deg",
            "rssi_db",
            "direct",
            "via",
            "precision_m",
            "raw",
        ):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.meta:
            out["meta"] = self.meta
        return out


def timestamp(seconds: float) -> str:
    """Return an RFC 3339 UTC time, to the millisecond."""
    when = datetime.fromtimestamp(seconds, UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _within(value: Any, low: float, high: float, digits: int) -> float | None:
    number = _number(value)
    if number is None or not low <= number <= high:
        return None
    return round(number, digits)


def observation(
    kind: str,
    address: str,
    t: float,
    lat: Any,
    lon: Any,
    *,
    alt_m: Any = None,
    alt_ref: str | None = None,
    gs_kn: Any = None,
    track_deg: Any = None,
    rssi_db: Any = None,
    direct: bool | None = None,
    via: str | None = None,
    precision_m: Any = None,
    raw: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Observation | None:
    """Return a checked observation, or None when it is not a usable point.

    The point needs an identity its kind allows, and a position. Every
    other field is dropped, not the point, when its value is outside the
    contract.
    """
    ident = address.strip().lower() if isinstance(address, str) else ""
    rule = ID_RULES.get(kind)
    if rule is None or not rule.match(ident):
        return None
    lat_v = _within(lat, -90, 90, 6)
    lon_v = _within(lon, -180, 180, 6)
    if lat_v is None or lon_v is None or not math.isfinite(t):
        return None
    alt = _within(alt_m, -500, 60000, 0)
    track = _number(track_deg)
    clean_meta = {
        k: v
        for k, v in (meta or {}).items()
        if v not in (None, "") and not (isinstance(v, str) and len(v) > MAX_META_VALUE_CHARS)
    }
    if len(json.dumps(clean_meta)) > MAX_META_BYTES:
        clean_meta = {}
    return Observation(
        kind=kind,
        id=ident,
        t=t,
        lat=lat_v,
        lon=lon_v,
        alt_m=alt,
        alt_ref=alt_ref if alt is not None and alt_ref in ("baro", "geom") else None,
        gs_kn=_within(gs_kn, 0, 2000, 1),
        track_deg=round(track % 360, 1) if track is not None else None,
        rssi_db=_within(rssi_db, -200, 50, 1),
        direct=direct if isinstance(direct, bool) else None,
        via=_station(via),
        precision_m=_within(precision_m, 0, 200_000, 0),
        # Too long to send whole is sent not at all, as with a comment.
        raw=raw if isinstance(raw, str) and 0 < len(raw) <= MAX_RAW_CHARS else None,
        meta=clean_meta,
    )


def _station(call: str | None) -> str | None:
    """A callsign we relayed through, checked the way an id is."""
    if not isinstance(call, str):
        return None
    ident = call.strip().lower()
    return ident if ID_RULES["aprs"].match(ident) and not ident.startswith(";") else None


def batch(
    station: str,
    kind: str,
    obs: list[Observation],
    sent: float,
    source: str | None = None,
) -> dict[str, Any]:
    """Return one batch message. The caller keeps it to MAX_OBS points."""
    if len(obs) > MAX_OBS:
        raise ValueError(f"a batch holds at most {MAX_OBS} observations")
    out: dict[str, Any] = {"v": VERSION, "station": station}
    if source is not None:
        out["source"] = source
    out.update(kind=kind, sent=timestamp(sent), obs=[o.to_json() for o in obs])
    return out


@dataclass
class Counts:
    """What the agent did since its last heartbeat.

    `heard` is every position a reader produced. `dropped` is every one
    thinning held back. `sent` is every one published to the broker.
    """

    heard: int = 0
    sent: int = 0
    dropped: int = 0

    def take(self) -> Counts:
        """Return the counts so far and start again from zero."""
        taken = Counts(self.heard, self.sent, self.dropped)
        self.heard = self.sent = self.dropped = 0
        return taken

    def give_back(self, other: Counts) -> None:
        """Add counts that were taken but could not be reported."""
        self.heard += other.heard
        self.sent += other.sent
        self.dropped += other.dropped


def status(
    station: str,
    sent: float,
    *,
    kinds: list[str],
    counts: Counts,
    agent_version: str,
    position: tuple[float, float] | None,
    precision_m: int,
    mesh_node: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Return the heartbeat. `position` must already be rounded."""
    out: dict[str, Any] = {"v": VERSION, "station": station}
    if source is not None:
        out["source"] = source
    out["sent"] = timestamp(sent)
    if position is not None:
        out["lat"], out["lon"] = position
        out["precision_m"] = precision_m
    out["kinds"] = kinds
    out["agent_version"] = agent_version[:64]
    out["counts"] = {"heard": counts.heard, "sent": counts.sent, "dropped": counts.dropped}
    if mesh_node is not None:
        out["mesh_node"] = mesh_node
    return out
