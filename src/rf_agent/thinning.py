"""Thin at the edge: one point per aircraft per interval (design 4.3).

A decoder reports an aircraft about once a second. The agent sends:

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


@dataclass(slots=True)
class _Visit:
    last_sent: float
    last_heard: float
    pending: Observation | None = None


class Thinner:
    def __init__(self, interval_s: float, counts: Counts, gap_s: float = VISIT_GAP_S) -> None:
        self.interval = interval_s
        self.gap = gap_s
        self.counts = counts
        self._visits: dict[tuple[str, str], _Visit] = {}

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
        if obs.t - visit.last_sent >= self.interval:
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
