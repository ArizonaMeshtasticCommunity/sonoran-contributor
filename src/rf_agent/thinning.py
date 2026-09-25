"""Thin at the edge, per kind (design 4.3).

An ADS-B decoder reports an aircraft about once a second, and a sonde
sends about once a second for a two hour flight. APRS and weather
sensors are slow already, so every packet goes as received.

For a kind with an interval, the agent sends:

- the first point of each visit, at once;
- then a point whenever `interval` seconds have passed since the last
  one it sent;
- and the last point of the visit, once the aircraft has been silent for
  `VISIT_GAP_S`, if that point was not sent already.

Everything else is dropped and counted. `heard` counts every point in,
`dropped` every point held back, so the heartbeat can say how thin the
picture is. Times are the receiver's, in seconds since the epoch.
"""

from __future__ import annotations

from dataclasses import dataclass

from rf_agent.messages import Counts, Observation

# Silence that ends a visit. readsb drops a position after 60 s.
VISIT_GAP_S = 60.0
# Seconds between two points of one thing, per kind. Zero sends every
# point: APRS and sensors are slow already. Aircraft use the configured
# ADSB_INTERVAL_S instead of the entry here.
INTERVALS = {"adsb": 20.0, "uat": 20.0, "sonde": 5.0, "aprs": 0.0, "ism": 0.0}


@dataclass(slots=True)
class _Visit:
    last_sent: float
    last_heard: float
    pending: Observation | None = None


class Thinner:
    def __init__(self, interval_s: float, counts: Counts, gap_s: float = VISIT_GAP_S) -> None:
        # `interval_s` is ADSB_INTERVAL_S, for aircraft. Every other kind
        # follows INTERVALS.
        self.intervals = dict(INTERVALS)
        self.intervals["adsb"] = self.intervals["uat"] = interval_s
        self.gap = gap_s
        self.counts = counts
        self._visits: dict[tuple[str, str], _Visit] = {}

    def interval_for(self, kind: str) -> float:
        return self.intervals.get(kind, 0.0)

    def __len__(self) -> int:
        return len(self._visits)

    def offer(self, obs: Observation) -> list[Observation]:
        """Take one point. Returns the points to send now, often none."""
        self.counts.heard += 1
        key = (obs.kind, obs.id)
        visit = self._visits.get(key)
        out: list[Observation] = []

        if visit is not None and obs.t - visit.last_heard > self.gap:
            # The last visit ended and `expire` has not run yet.
            if visit.pending is not None:
                out.append(visit.pending)
            visit = None
        if visit is None:
            self._visits[key] = _Visit(last_sent=obs.t, last_heard=obs.t)
            out.append(obs)
            return out

        if obs.t < visit.last_heard:
            # Older than what we have, from a second source of the same
            # kind. It adds nothing.
            self.counts.dropped += 1
            return out
        visit.last_heard = obs.t
        # An interval of zero sends every point: the difference is never
        # below zero, so nothing is ever held back.
        if obs.t - visit.last_sent >= self.interval_for(obs.kind):
            if visit.pending is not None:
                self.counts.dropped += 1
                visit.pending = None
            visit.last_sent = obs.t
            out.append(obs)
            return out
        if visit.pending is not None:
            self.counts.dropped += 1
        visit.pending = obs
        return out

    def expire(self, now: float) -> list[Observation]:
        """End every visit silent for longer than the gap. Returns their last points."""
        out: list[Observation] = []
        for key in [k for k, v in self._visits.items() if now - v.last_heard > self.gap]:
            visit = self._visits.pop(key)
            if visit.pending is not None:
                out.append(visit.pending)
        return out

    def flush(self) -> list[Observation]:
        """End every visit, when the agent stops. Returns their last points."""
        out = [v.pending for v in self._visits.values() if v.pending is not None]
        self._visits.clear()
        return out
