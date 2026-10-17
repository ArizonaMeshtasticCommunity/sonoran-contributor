"""Skeleton for the red step."""

from typing import Any

MAX_FRAME = 0


def encode_frame(payload: bytes) -> bytes:
    raise NotImplementedError


class FrameReader:
    def feed(self, data: bytes) -> list[bytes]:
        raise NotImplementedError


class ProxyHub:
    ready = False

    def __init__(self, send_upstream: Any) -> None:
        pass

    def on_from_radio(self, raw: bytes) -> None:
        raise NotImplementedError

    def handle_client(self, raw: bytes) -> list[bytes]:
        raise NotImplementedError

    def subscribe(self, sink: Any) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


async def serve(hub: ProxyHub, bind: str, port: int) -> Any:
    raise NotImplementedError
