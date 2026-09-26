"""Read `radiosonde_auto_rx`: one JSON summary per datagram.

`auto_rx` broadcasts a summary of each sonde it is tracking, on UDP
55672, in the format Horus uses:

    {"type": "PAYLOAD_SUMMARY", "callsign": "S1234567",
     "latitude": 33.6, "longitude": -112.1, "altitude": 18450,
     "speed": 42.0, "heading": 88.0, "model": "RS41", ...}

The sonde reports its own GPS position, so the point is where the thing
is. Its `callsign` is the serial printed on the radiosonde, and that is
the identity we keep.

A flight lasts about two hours and reports about once a second;
thinning cuts that to one point every five seconds (design 4.3).

Anything that is not a payload summary, such as a station announcement,
is ignored. `auto_rx` must have its internet uploads off: this agent
sends what our own receiver heard, never what a network told it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from rf_agent.messages import Observation, observation

logger = logging.getLogger(__name__)

# Horus sends the speed in km/h.
KMH_PER_KNOT = 1.852
SUMMARY = "PAYLOAD_SUMMARY"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class AutoRx:
    """Turn `auto_rx` summaries into observations."""

    def __init__(self, kind: str = "sonde") -> None:
        self.kind = kind
        self.bad = 0
        self.ignored = 0

    def feed(self, data: bytes, now: float) -> list[Observation]:
        """Handle one datagram. A bad one is counted, never raised."""
        try:
            point = self.summary(data, now)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            self.bad += 1
            return []
        return [point] if point is not None else []

    def summary(self, data: bytes, now: float) -> Observation | None:
        message = json.loads(data)
        if not isinstance(message, dict):
            raise ValueError("not a JSON object")
        if message.get("type") != SUMMARY:
            self.ignored += 1
            return None

        speed = _num(message.get("speed"))
        # `auto_rx` stamps a time of day with no date, so the time we
        # received it is the one we can place.
        point = observation(
            self.kind,
            str(message.get("callsign", "")),
            now,
            message.get("latitude"),
            message.get("longitude"),
            # A sonde's altitude is metres above the ellipsoid, from GPS.
            alt_m=message.get("altitude"),
            alt_ref="geom",
            gs_kn=speed / KMH_PER_KNOT if speed is not None else None,
            track_deg=message.get("heading"),
            raw=data.decode("utf-8", "replace").strip(),
            # `snr` is a signal to noise ratio, not a level. It is kept in
            # meta rather than passed off as rssi_db.
            meta={
                "model": message.get("model"),
                "freq": message.get("freq"),
                "sats": message.get("sats"),
                "snr": message.get("snr"),
            },
        )
        if point is None:
            self.bad += 1
        return point
