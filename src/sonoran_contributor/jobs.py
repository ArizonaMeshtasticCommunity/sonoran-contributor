"""Do one job with the member's own node (Sonoran #178, #179).

The node is behind `Link`, which offers one thing: ask a node for its
LocalStats and wait for the answer. There is no way here to send an
admin message or change a setting, on purpose.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sonoran_contributor.contract import Job, iso, node_id, result_message

# The longest wait for an answer: the Meshtastic Python library's own,
# MeshInterface(timeout=300) (meshtastic/mesh_interface.py line 93,
# meshtastic/python 2.7.11). A job's `expires` can make it shorter.
JOB_WAIT_S = 300.0
# The largest node list a radio keeps: MAX_NUM_NODES on an ESP32-S3 with
# 16 MB flash (src/mesh/mesh-pb-constants.h, meshtastic/firmware). The
# contract allows no more.
MAX_HEARD = 250
# How many job ids are remembered. A retained job arrives again on each
# reconnect; Sonoran clears it within minutes, so a small memory is
# enough. This is a memory cap, not a measure.
SEEN_LIMIT = 1024

# The LocalStats fields, by their names in meshtastic/telemetry.proto.
LOCAL_STATS_FIELDS = (
    "uptime_seconds",
    "channel_utilization",
    "air_util_tx",
    "num_packets_tx",
    "num_packets_rx",
    "num_packets_rx_bad",
    "num_online_nodes",
    "num_total_nodes",
    "num_rx_dupe",
    "num_tx_relay",
    "num_tx_relay_canceled",
    "heap_total_bytes",
    "heap_free_bytes",
    "num_tx_dropped",
    "noise_floor",
)


class LinkError(Exception):
    """The node could not do the request. The text goes in `detail`."""


@dataclass(frozen=True)
class Reply:
    """A LocalStats answer as our node received it."""

    local_stats: dict[str, Any]
    received: datetime
    hops: int | None = None
    rx_snr: float | None = None
    rx_rssi: int | None = None


class Link(Protocol):
    async def request_local_stats(self, target: int, wait_s: float) -> Reply | None:
        """Ask `target` for LocalStats. None when no answer came in `wait_s`."""
        ...


class Seen:
    """Job ids already started, so a retained job is done once."""

    def __init__(self, limit: int = SEEN_LIMIT) -> None:
        self.limit = limit
        self._ids: OrderedDict[str, None] = OrderedDict()

    def first(self, job: str) -> bool:
        if job in self._ids:
            return False
        self._ids[job] = None
        while len(self._ids) > self.limit:
            self._ids.popitem(last=False)
        return True


def _now() -> datetime:
    return datetime.now(UTC)


async def do_job(
    job: Job,
    link: Link,
    allow: frozenset[str],
    location: str,
    source: str,
    now: Callable[[], datetime] = _now,
) -> dict[str, Any] | None:
    """Do one job. The result to publish, or None for an expired job."""
    started = now()
    if job.expires <= started:
        return None
    if job.kind not in allow:
        return result_message(job, location, source, "refused", started)
    wait = min(JOB_WAIT_S, (job.expires - started).total_seconds())
    try:
        reply = await link.request_local_stats(job.target_num, wait)
    except LinkError as exc:
        return result_message(job, location, source, "error", now(), detail=str(exc)[:200])
    except Exception as exc:
        # Anything else from the node, such as the OSError of a dead
        # socket, is the job's error. It must never stop the process,
        # which may run RF too (audit F1).
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return result_message(job, location, source, "error", now(), detail=detail)
    sent = now()
    if reply is None:
        return result_message(job, location, source, "no_reply", sent)
    stats = {key: reply.local_stats[key] for key in LOCAL_STATS_FIELDS if key in reply.local_stats}
    return result_message(
        job,
        location,
        source,
        "ok",
        sent,
        received=iso(min(reply.received, sent)),
        local_stats=stats,
        hops=reply.hops,
        rx_snr=reply.rx_snr,
        rx_rssi=reply.rx_rssi,
    )


def heard_list(
    nodes: Iterable[tuple[int, int | None, float | None]],
    now: datetime,
) -> list[dict[str, Any]]:
    """The heartbeat's heard list: (node, hops, last heard epoch), newest first.

    A node with no last-heard time is left out. A time after `now` is the
    node's clock, and is sent as `now`.
    """
    known = [(num, hops, heard) for num, hops, heard in nodes if heard]
    known.sort(key=lambda item: item[2] or 0.0, reverse=True)
    out = []
    for num, hops, heard in known[:MAX_HEARD]:
        moment = min(datetime.fromtimestamp(heard or 0.0, UTC), now)
        out.append({"node": node_id(num), "hops": hops, "last_heard": iso(moment)})
    return out
