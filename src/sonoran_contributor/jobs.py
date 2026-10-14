"""Skeleton for the red step."""

from dataclasses import dataclass
from typing import Any

JOB_WAIT_S = 0.0
MAX_HEARD = 0


class LinkError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    local_stats: dict[str, Any]
    received: Any
    hops: int | None = None
    rx_snr: float | None = None
    rx_rssi: int | None = None


class Seen:
    def __init__(self, limit: int = 0) -> None:
        pass

    def first(self, job: str) -> bool:
        raise NotImplementedError


async def do_job(*args: Any) -> dict[str, Any] | None:
    raise NotImplementedError


def heard_list(*args: Any) -> list[dict[str, Any]]:
    raise NotImplementedError
