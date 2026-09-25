"""Read `rtl_433`: one JSON object per datagram, allow-listed by model.

`rtl_433` sends each decoded device as JSON, either bare or wrapped in a
syslog line (`-F syslog:host:port`):

    {"time": "2026-09-25 06:00:00", "model": "Acurite-Tower",
     "id": 1234, "channel": "A", "temperature_C": 21.5, "humidity": 44}

**Nothing passes until a member names the models** (design 8).
`rtl_433` decodes tyre sensors, doorbells and car keys in neighbours'
houses as readily as weather stations, so `ISM_ALLOW` is empty by
default and a model absent from it is dropped and counted. Sonoran
applies the same list again.

A weather sensor transmits no position. The point carries the station's
**rounded** position instead, so it says where it was heard, not where
it is. Every sensor one station hears lands on the same point.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from rf_agent.messages import Observation, observation

logger = logging.getLogger(__name__)

# The readings worth keeping, as `rtl_433` names them, lower-cased here.
READINGS = {
    "temperature_C": "temperature_c",
    "temperature_F": "temperature_f",
    "humidity": "humidity",
    "pressure_hPa": "pressure_hpa",
    "wind_avg_km_h": "wind_avg_kmh",
    "wind_max_km_h": "wind_max_kmh",
    "wind_dir_deg": "wind_dir_deg",
    "rain_mm": "rain_mm",
    "battery_ok": "battery_ok",
}
UNSAFE = re.compile(r"[^a-z0-9_.-]+")


def parse_allow(text: str) -> frozenset[str]:
    """Parse ISM_ALLOW: model names, comma separated, case ignored."""
    return frozenset(part.strip().lower() for part in text.split(",") if part.strip())


def thing_id(model: str, device: Any, channel: Any) -> str | None:
    """The identity of one sensor: its model, then its id.

    Sonoran checks that the id starts with the model, so the two must
    agree. A device with neither id nor channel gets none: a made-up
    identity would merge two sensors into one thing.
    """
    tail = device if device not in (None, "") else channel
    if tail in (None, ""):
        return None
    ident = UNSAFE.sub("-", f"{model.strip().lower()}-{str(tail).strip().lower()}")
    return re.sub(r"-{2,}", "-", ident).strip("-") or None


class Rtl433:
    """Turn `rtl_433` JSON into observations, for allowed models only."""

    def __init__(
        self,
        kind: str = "ism",
        allow: frozenset[str] = frozenset(),
        position: tuple[float, float] | None = None,
    ) -> None:
        self.kind = kind
        self.allow = allow
        # The station's rounded position, the one the heartbeat carries.
        self.position = position
        self.bad = 0
        self.not_allowed = 0
        self._told: set[str] = set()

    def feed(self, data: bytes, now: float) -> list[Observation]:
        try:
            point = self.device(data, now)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            self.bad += 1
            return []
        return [point] if point is not None else []

    def device(self, data: bytes, now: float) -> Observation | None:
        text = data.decode("utf-8", "replace")
        if not text.startswith("{"):
            # A syslog line wraps the JSON; the object starts at the brace.
            start = text.find("{")
            if start < 0:
                raise ValueError("no JSON object in the datagram")
            text = text[start:]
        message = json.loads(text)
        if not isinstance(message, dict):
            raise ValueError("not a JSON object")

        model = message.get("model")
        if not isinstance(model, str) or not model.strip():
            self.bad += 1
            return None
        if model.strip().lower() not in self.allow:
            self.not_allowed += 1
            if model not in self._told:
                self._told.add(model)
                logger.info(
                    "ism: %r is not in ISM_ALLOW, so nothing from it is sent", model.strip()
                )
            return None
        if self.position is None:
            # Nothing to place it at. Config refuses this combination, so
            # this is only reachable in a test.
            self.bad += 1
            return None

        ident = thing_id(model, message.get("id"), message.get("channel"))
        if ident is None:
            self.bad += 1
            return None

        meta: dict[str, Any] = {"model": model.strip(), "id": message.get("id")}
        if message.get("channel") not in (None, ""):
            meta["channel"] = message["channel"]
        for name, key in READINGS.items():
            if name in message and not isinstance(message[name], dict | list):
                meta[key] = message[name]

        point = observation(
            self.kind,
            ident,
            now,
            self.position[0],
            self.position[1],
            rssi_db=message.get("rssi"),
            meta=meta,
        )
        if point is None:
            self.bad += 1
        return point
