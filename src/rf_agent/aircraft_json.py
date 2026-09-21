"""Read `aircraft.json`, the snapshot readsb and dump1090-fa keep current.

The agent reads it about once a second, over HTTP or from the file
itself. Each snapshot lists every aircraft the decoder knows, with how
many seconds ago its position was last heard (`seen_pos`). A position
seen in an earlier snapshot is not taken again.

Only positions our own receiver heard pass. readsb can also hold
positions it did not hear: MLAT results computed by an aggregator and
fed back to it, and ADS-C relayed from the internet. Both are left out,
by the `type` readsb gives each aircraft and by its `mlat` field list.
"""

from __future__ import annotations

import json
from typing import Any

from rf_agent.messages import FEET, Observation, observation

# Types whose position was not heard by this receiver.
NOT_HEARD_HERE = frozenset({"mlat", "adsc", "other", "mode_s", "unknown"})
# readsb keeps a position for 60 s after it was last heard.
MAX_POSITION_AGE_S = 60.0
FORGET_S = 600.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class AircraftJson:
    """Turn successive `aircraft.json` snapshots into new positions."""

    def __init__(self, kind: str = "adsb") -> None:
        self.kind = kind
        self._last: dict[str, float] = {}  # address -> time of its last position
        self.bad = 0
        self.excluded = 0

    def parse(self, data: bytes, now: float) -> list[Observation]:
        """Parse one snapshot. Raises ValueError when it is not one."""
        doc = json.loads(data)
        if not isinstance(doc, dict) or not isinstance(doc.get("aircraft"), list):
            raise ValueError("not an aircraft.json document")
        snapshot = _num(doc.get("now")) or now
        out: list[Observation] = []
        for ac in doc["aircraft"]:
            try:
                point = self._aircraft(ac, snapshot)
            except (TypeError, ValueError, AttributeError):
                self.bad += 1
                continue
            if point is not None:
                out.append(point)
        for address in [a for a, t in self._last.items() if snapshot - t > FORGET_S]:
            del self._last[address]
        return out

    def _aircraft(self, ac: Any, snapshot: float) -> Observation | None:
        if not isinstance(ac, dict) or "lat" not in ac or "lon" not in ac:
            return None
        if ac.get("type") in NOT_HEARD_HERE or "lat" in (ac.get("mlat") or ()):
            self.excluded += 1
            return None
        address = str(ac.get("hex", "")).strip().lower()
        age = _num(ac.get("seen_pos"))
        age = 0.0 if age is None else age
        if age > MAX_POSITION_AGE_S:
            return None
        t = snapshot - age
        last = self._last.get(address)
        if last is not None and t <= last + 0.05:
            return None  # the same position as in an earlier snapshot

        alt_baro = ac.get("alt_baro", ac.get("altitude"))
        on_ground = alt_baro == "ground"
        alt_m = alt_ref = None
        if not on_ground:
            if _num(alt_baro) is not None:
                alt_m, alt_ref = _num(alt_baro) * FEET, "baro"
            elif _num(ac.get("alt_geom")) is not None:
                alt_m, alt_ref = _num(ac.get("alt_geom")) * FEET, "geom"

        flight = ac.get("flight")
        meta = {
            "callsign": flight.strip() if isinstance(flight, str) else None,
            "squawk": ac.get("squawk") if isinstance(ac.get("squawk"), str) else None,
            "category": ac.get("category") if isinstance(ac.get("category"), str) else None,
            "ground": True if on_ground else None,
        }
        point = observation(
            self.kind,
            address,
            t,
            ac.get("lat"),
            ac.get("lon"),
            alt_m=alt_m,
            alt_ref=alt_ref,
            gs_kn=ac.get("gs", ac.get("speed")),
            track_deg=ac.get("track"),
            rssi_db=ac.get("rssi"),
            meta=meta,
        )
        if point is None:
            self.bad += 1
            return None
        self._last[point.id] = t
        return point
