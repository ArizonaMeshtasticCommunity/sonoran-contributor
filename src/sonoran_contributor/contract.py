"""Skeleton for the red step."""

from dataclasses import dataclass
from typing import Any

TOPIC_ROOT = ""


@dataclass(frozen=True)
class Job:
    job: str

    def as_dict(self) -> dict[str, Any]:
        raise NotImplementedError


def outbox_filter(location: str, source: str) -> str:
    raise NotImplementedError


def inbox_topic(location: str, source: str, job: str) -> str:
    raise NotImplementedError


def status_topic(location: str, source: str) -> str:
    raise NotImplementedError


def parse_job(payload: bytes, topic_job: str) -> Job | None:
    raise NotImplementedError


def status_message(*args: Any) -> dict[str, Any]:
    raise NotImplementedError
