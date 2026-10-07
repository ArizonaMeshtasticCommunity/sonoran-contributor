"""Member mods: points a member's own program sends through the agent (#324).

A member may write a program that reads their own receiver, a decoder we
do not support or an experiment, and send what it hears as their
station's data. It sends JSON lines to the agent over UDP, one point a
line:

    {"mod": "lightning", "id": "strike-1", "t": 1759600000.5,
     "heard_here": true, "meta": {"distance_km": 12}}

`t` is seconds since the epoch or an RFC 3339 time; without it, the
time the agent got the line. A point gives `lat` and `lon`, or says it
was `heard_here`, or neither. See `docs/MODS.md`.

The agent checks each line, keeps one point per thing per
`CUSTOM_INTERVAL_S`, holds a mod to `CUSTOM_PER_MINUTE` points, and
sends the rest in batches on `rf/v1/<station>/custom`. So a mod needs no
broker login of its own, and the station keeps one heartbeat.

The checks here are for honest members. Sonoran checks every point
again and trusts none of it: a mod gets no more than a built-in decoder.

Like `rtl_433` and `radiosonde_auto_rx`, this is read from a UDP socket,
which accepts no connection and serves nothing. Every datagram is
untrusted input: a bad one is counted and skipped.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from rf_agent.dump978 import LineSplitter
from rf_agent.messages import MAX_OBS, MOD_NAME, Observation, custom_batch, custom_point

logger = logging.getLogger(__name__)

# The longest line a mod may send. A point with a full `meta` is about 5 kB.
MAX_LINE_BYTES = 8192
# Things remembered for thinning, across every mod.
MAX_THINGS = 10_000
# Mods one agent sends for at once.
MAX_MODS = 16


def _when(value: Any, now: float) -> float:
    if isinstance(value, bool):
        return now
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return now
    return now


class CustomInput:
    """Read mods' JSON lines; produce batches of their points."""

    def __init__(self, station: str, interval_s: float = 10.0, per_minute: int = 600) -> None:
        self.station = station
        self.interval_s = interval_s
        self.per_minute = per_minute
        self.lines = LineSplitter()
        self.bad = 0
        self.thinned = 0
        self.over = 0
        self._sent: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._window = 0
        self._in_window: dict[str, int] = {}
        self._warned: set[str] = set()

    def feed(self, data: bytes, now: float) -> list[Observation]:
        """Handle a datagram. A mod's points are not observations: see `tick`."""
        if not data.endswith(b"\n"):
            # One datagram is one or more whole lines.
            data += b"\n"
        for line in self.lines.feed(data):
            if len(line) > MAX_LINE_BYTES:
                self.bad += 1
                continue
            try:
                self.line(json.loads(line), now)
            except (ValueError, TypeError):
                self.bad += 1
        return []

    def line(self, item: Any, now: float) -> None:
        if not isinstance(item, dict):
            raise ValueError("not an object")
        mod = item.get("mod")
        if not isinstance(mod, str) or not MOD_NAME.match(mod):
            raise ValueError("no mod name")
        point = custom_point(
            item.get("id"),
            _when(item.get("t"), now),
            lat=item.get("lat"),
            lon=item.get("lon"),
            heard_here=item.get("heard_here") is True,
            meta=item.get("meta"),
        )
        if point is None:
            raise ValueError("not a point")
        key = (mod, point["id"])
        last = self._sent.get(key)
        if last is not None and now - last < self.interval_s:
            self.thinned += 1
            return
        window = int(now // 60)
        if window != self._window:
            self._window, self._in_window = window, {}
        if self._in_window.get(mod, 0) >= self.per_minute:
            self.over += 1
            if mod not in self._warned:
                self._warned.add(mod)
                logger.warning(
                    "custom: mod %s sends more than %d points a minute; the rest are dropped",
                    mod,
                    self.per_minute,
                )
            return
        if mod not in self._pending and len(self._pending) >= MAX_MODS:
            self.over += 1
            return
        self._in_window[mod] = self._in_window.get(mod, 0) + 1
        self._sent[key] = now
        self._sent.move_to_end(key)
        while len(self._sent) > MAX_THINGS:
            self._sent.popitem(last=False)
        self._pending.setdefault(mod, []).append(point)

    def tick(self, now: float | None = None) -> list[dict[str, Any]]:
        """Return every waiting point, as batches ready to publish."""
        sent = time.time() if now is None else now
        out: list[dict[str, Any]] = []
        for mod, points in self._pending.items():
            for start in range(0, len(points), MAX_OBS):
                out.append(custom_batch(self.station, mod, points[start : start + MAX_OBS], sent))
        self._pending = {}
        return out
