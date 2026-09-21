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
# Sonoran caps `meta` at 1 kB. Ours is a few short strings; this is a guard.
MAX_META_BYTES = 512
# An ICAO address as readsb prints it. A leading `~` marks one that is not
# an ICAO address, such as a TIS-B target. Sonoran stores nothing else.
AIRCRAFT_ID = re.compile(r"^~?[0-9a-f]{6}$")
STATION_ID = re.compile(r"^[a-z0-9-]{3,32}$")
AIRCRAFT_KINDS = ("adsb", "uat")
FEET = 0.3048


@dataclass(frozen=True, slots=True)
class Observation:
    """One aircraft heard, at one moment. `t` is seconds since the epoch."""

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
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Return the observation as the contract spells it."""
        out: dict[str, Any] = {
            "id": self.id,
            "t": timestamp(self.t),
            "lat": self.lat,
            "lon": self.lon,
        }
        for name in ("alt_m", "alt_ref", "gs_kn", "track_deg", "rssi_db"):
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
    meta: dict[str, Any] | None = None,
) -> Observation | None:
    """Return a checked observation, or None when it is not a usable point.

    The point needs an aircraft address and a position. Every other field
    is dropped, not the point, when its value is outside the contract.
    """
    ident = address.strip().lower() if isinstance(address, str) else ""
    if not AIRCRAFT_ID.match(ident):
        return None
    lat_v = _within(lat, -90, 90, 6)
    lon_v = _within(lon, -180, 180, 6)
    if lat_v is None or lon_v is None or not math.isfinite(t):
        return None
    alt = _within(alt_m, -500, 60000, 0)
    track = _number(track_deg)
    clean_meta = {k: v for k, v in (meta or {}).items() if v not in (None, "")}
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
        meta=clean_meta,
    )


def batch(station: str, kind: str, obs: list[Observation], sent: float) -> dict[str, Any]:
    """Return one batch message. The caller keeps it to MAX_OBS points."""
    if len(obs) > MAX_OBS:
        raise ValueError(f"a batch holds at most {MAX_OBS} observations")
    return {
        "v": VERSION,
        "station": station,
        "kind": kind,
        "sent": timestamp(sent),
        "obs": [o.to_json() for o in obs],
    }


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
) -> dict[str, Any]:
    """Return the heartbeat. `position` must already be rounded."""
    out: dict[str, Any] = {
        "v": VERSION,
        "station": station,
        "sent": timestamp(sent),
    }
    if position is not None:
        out["lat"], out["lon"] = position
        out["precision_m"] = precision_m
    out["kinds"] = kinds
    out["agent_version"] = agent_version[:64]
    out["counts"] = {"heard": counts.heard, "sent": counts.sent, "dropped": counts.dropped}
    return out
