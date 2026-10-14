"""The contributor's side of Sonoran's contract, version 1 (Sonoran #178).

`schema/contributor-message-v1.schema.json` is the contract. It is
copied from Sonoran's `sonoran/contrib/schema.py`; never edit it here.
The tests check every message this module builds against it.

    sonoran/v1/contributors/<location>/<source>/outbox/<job>   Sonoran -> us, retained
    sonoran/v1/contributors/<location>/<source>/inbox/<job>    us -> Sonoran, QoS 1
    sonoran/v1/contributors/<location>/<source>/status         us -> Sonoran, retained
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TOPIC_ROOT = "sonoran/v1/contributors"
JOB_ID = re.compile(r"^[0-9a-f]{32}$")
NODE_ID = re.compile(r"^![0-9a-f]{8}$")
# The job kinds this contributor knows how to do.
KNOWN_KINDS = frozenset({"local_stats"})


def outbox_filter(location: str, source: str) -> str:
    """The subscription for this contributor's jobs, and only its own."""
    return f"{TOPIC_ROOT}/{location}/{source}/outbox/+"


def inbox_topic(location: str, source: str, job: str) -> str:
    return f"{TOPIC_ROOT}/{location}/{source}/inbox/{job}"


def status_topic(location: str, source: str) -> str:
    return f"{TOPIC_ROOT}/{location}/{source}/status"


def node_id(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def iso(moment: datetime) -> str:
    return moment.isoformat()


@dataclass(frozen=True)
class Job:
    """One job from the outbox."""

    job: str
    kind: str
    target: str
    issued: datetime
    expires: datetime

    @property
    def target_num(self) -> int:
        return int(self.target[1:], 16)

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "job": self.job,
            "kind": self.kind,
            "target": self.target,
            "issued": iso(self.issued),
            "expires": iso(self.expires),
        }


def _moment(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a time")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError(f"{name} has no time zone")
    return moment


def parse_job(payload: bytes, topic_job: str) -> Job | None:
    """Read one outbox message. None for an empty one: a cleared job.

    Raises ValueError for anything that is not a job of this contract,
    or whose id is not the one its topic names. The kind is checked
    against the allow list later, so a kind we do not do is answered
    `refused` rather than ignored.
    """
    if not payload:
        return None
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError("not a version 1 job")
    job = data.get("job")
    if not isinstance(job, str) or not JOB_ID.match(job) or job != topic_job:
        raise ValueError("the job id does not match its topic")
    target = data.get("target")
    if not isinstance(target, str) or not NODE_ID.match(target):
        raise ValueError("the target is not a node id")
    kind = data.get("kind")
    if not isinstance(kind, str):
        raise ValueError("the job has no kind")
    return Job(
        job=job,
        kind=kind,
        target=target,
        issued=_moment(data.get("issued"), "issued"),
        expires=_moment(data.get("expires"), "expires"),
    )


def result_message(
    job: Job,
    location: str,
    source: str,
    outcome: str,
    sent: datetime,
    **extra: Any,
) -> dict[str, Any]:
    """A result. `extra` holds `received`, `local_stats`, `hops` and so on."""
    message: dict[str, Any] = {
        "v": 1,
        "job": job.job,
        "location": location,
        "source": source,
        "kind": job.kind,
        "target": job.target,
        "outcome": outcome,
        "sent": iso(sent),
    }
    message.update({key: value for key, value in extra.items() if value is not None})
    return message


def status_message(
    location: str,
    source: str,
    sent: datetime,
    version: str,
    mesh_node: int | None,
    kinds: frozenset[str],
    heard: list[dict[str, Any]],
) -> dict[str, Any]:
    """The heartbeat."""
    message: dict[str, Any] = {
        "v": 1,
        "location": location,
        "source": source,
        "sent": iso(sent),
        "version": version,
        "kinds": sorted(kinds & KNOWN_KINDS),
        "heard": heard,
    }
    if mesh_node is not None:
        message["mesh_node"] = node_id(mesh_node)
    return message
