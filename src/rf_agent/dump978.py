"""Read dump978-fa's decoded JSON: one UAT message per line.

dump978-fa serves two outputs. The JSON one, port 30979 by default, is
the one read here. The raw one, port 30978 by default, carries hex
frames that start with `-` or `+`; a line like that is counted and
reported once, because it means the source points at the wrong port.

Not every UAT message carries the callsign, so the last one heard is
kept per aircraft for a minute and added to its positions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from rf_agent.messages import FEET, Observation, observation

logger = logging.getLogger(__name__)

# The UAT address qualifier: which kind of address this is, as readsb
# would print it. Ground vehicles and fixed beacons are not aircraft.
QUALIFIERS = {
    "adsb_icao": "",
    "tisb_icao": "",
    "adsb_other": "~",
    "tisb_trackfile": "~",
    "tisb_other": "~",
    "adsr_other": "~",
}
STALE_S = 60.0
MAX_LINE = 1 << 16


class LineSplitter:
    """Cut a byte stream into lines, keeping a partial line between reads."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.too_long = 0

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        *lines, rest = self._buf.split(b"\n")
        self._buf = bytearray(rest)
        if len(self._buf) > MAX_LINE:
            self._buf.clear()
            self.too_long += 1
        return [bytes(line).strip() for line in lines if line.strip()]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class Dump978:
    """Turn dump978-fa JSON lines into observations."""

    def __init__(self, kind: str = "uat") -> None:
        self.kind = kind
        self.lines = LineSplitter()
        self.bad = 0
        self.raw_lines = 0
        self._names: dict[str, tuple[dict[str, Any], float]] = {}

    def feed(self, data: bytes, now: float) -> list[Observation]:
        out: list[Observation] = []
        for line in self.lines.feed(data):
            try:
                point = self.line(line, now)
            except (ValueError, TypeError, AttributeError):
                self.bad += 1
                continue
            if point is not None:
                out.append(point)
        return out

    def line(self, line: bytes, now: float) -> Observation | None:
        if line[:1] in (b"-", b"+"):
            if not self.raw_lines:
                logger.warning(
                    "uat source sends dump978 raw frames; point it at the JSON port "
                    "(30979 by default) instead"
                )
            self.raw_lines += 1
            return None
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("not a JSON object")
        prefix = QUALIFIERS.get(msg.get("address_qualifier", "adsb_icao"))
        address = msg.get("address")
        if prefix is None or not isinstance(address, str):
            return None
        address = prefix + address.strip().lower()

        meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        t = _num(meta.get("received_at")) or _num(msg.get("received_at")) or now

        names = self._remember(address, msg, now)
        position = msg.get("position")
        if not isinstance(position, dict):
            return None

        on_ground = msg.get("airground_state") == "ground"
        alt_m = alt_ref = None
        if not on_ground:
            if _num(msg.get("pressure_altitude")) is not None:
                alt_m, alt_ref = _num(msg["pressure_altitude"]) * FEET, "baro"
            elif _num(msg.get("geometric_altitude")) is not None:
                alt_m, alt_ref = _num(msg["geometric_altitude"]) * FEET, "geom"

        point = observation(
            self.kind,
            address,
            t,
            position.get("lat"),
            position.get("lon"),
            alt_m=alt_m,
            alt_ref=alt_ref,
            gs_kn=msg.get("ground_speed"),
            track_deg=msg.get("true_track"),
            rssi_db=meta.get("rssi"),
            raw=line.decode("utf-8", "replace"),
            meta={**names, "ground": True if on_ground else None},
        )
        if point is None:
            self.bad += 1
        return point

    def _remember(self, address: str, msg: dict[str, Any], now: float) -> dict[str, Any]:
        """Keep the callsign, squawk and category, which come now and then."""
        names: dict[str, Any] = {}
        if isinstance(msg.get("callsign"), str) and msg["callsign"].strip():
            names["callsign"] = msg["callsign"].strip()
        # A UAT "flight plan ID" is the squawk.
        if isinstance(msg.get("flightplan_id"), str) and msg["flightplan_id"].strip():
            names["squawk"] = msg["flightplan_id"].strip()
        if isinstance(msg.get("emitter_category"), str):
            names["category"] = msg["emitter_category"]
        known, when = self._names.get(address, ({}, 0.0))
        if now - when > STALE_S and not names:
            known = {}
        if names:
            known = {**(known if now - when <= STALE_S else {}), **names}
            self._names[address] = (known, now)
        if len(self._names) > 5000:
            for key in [k for k, (_, w) in self._names.items() if now - w > STALE_S]:
                del self._names[key]
        return known
