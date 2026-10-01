"""The broker's admin API, as far as the registrar uses it.

EMQX 5. The registrar keeps its logins in the built-in user database
and its rules in the built-in authorization database. That source must
come before any file source, so a login's own rules decide first.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

AUTHN = quote("password_based:built_in_database", safe="")
RULES = "authorization/sources/built_in_database/rules/users"


class BrokerError(RuntimeError):
    """The broker refused a request, or could not be reached."""


class Broker(Protocol):
    async def create_user(self, username: str, password: str) -> None: ...
    async def set_password(self, username: str, password: str) -> None: ...
    async def delete_user(self, username: str) -> None: ...
    async def set_rules(self, username: str, rules: list[dict[str, str]]) -> None: ...
    async def delete_rules(self, username: str) -> None: ...
    async def kick(self, username: str) -> int: ...
    async def publish_retained(self, topic: str, payload: bytes) -> None: ...


def rules_for(station: str, source: str) -> list[dict[str, str]]:
    """The rules of one source's login, in order. The first match decides.

    The login may publish its own heartbeat and the station's data
    topics. It may not publish another source's heartbeat, the legacy
    heartbeat, or the registry. It subscribes to nothing. `status/#`
    also matches `status` itself.
    """
    base = f"rf/v1/{station}"
    return [
        {"permission": "deny", "action": "publish", "topic": f"{base}/registry"},
        {"permission": "allow", "action": "publish", "topic": f"{base}/status/{source}"},
        {"permission": "deny", "action": "publish", "topic": f"{base}/status/#"},
        {"permission": "allow", "action": "publish", "topic": f"{base}/+"},
        {"permission": "deny", "action": "all", "topic": "#"},
    ]


class Emqx:
    """The EMQX 5 REST API, with an API key."""

    def __init__(self, url: str, key: str, secret: str, session: aiohttp.ClientSession) -> None:
        self.url = url
        self.auth = aiohttp.BasicAuth(key, secret)
        self.session = session

    async def _call(
        self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 204)
    ) -> Any:
        try:
            async with self.session.request(
                method,
                f"{self.url}/{path}",
                json=body,
                auth=self.auth,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                text = await response.text()
                if response.status not in ok:
                    raise BrokerError(f"{method} {path}: {response.status} {text[:200]}")
                return json.loads(text) if text else None
        except aiohttp.ClientError as exc:
            raise BrokerError(f"{method} {path}: {exc}") from exc

    async def create_user(self, username: str, password: str) -> None:
        await self._call(
            "POST",
            f"authentication/{AUTHN}/users",
            {"user_id": username, "password": password, "is_superuser": False},
        )

    async def set_password(self, username: str, password: str) -> None:
        await self._call(
            "PUT", f"authentication/{AUTHN}/users/{quote(username)}", {"password": password}
        )

    async def delete_user(self, username: str) -> None:
        await self._call("DELETE", f"authentication/{AUTHN}/users/{quote(username)}", ok=(204, 404))

    async def set_rules(self, username: str, rules: list[dict[str, str]]) -> None:
        # POST adds a new entry and fails on an old one; PUT replaces one.
        try:
            await self._call("POST", RULES, [{"username": username, "rules": rules}])
        except BrokerError:
            await self._call(
                "PUT", f"{RULES}/{quote(username)}", {"username": username, "rules": rules}
            )

    async def delete_rules(self, username: str) -> None:
        await self._call("DELETE", f"{RULES}/{quote(username)}", ok=(204, 404))

    async def kick(self, username: str) -> int:
        """Disconnect every client of a login. Returns how many."""
        found = await self._call("GET", f"clients?username={quote(username)}&limit=100")
        clients = [row["clientid"] for row in (found or {}).get("data", [])]
        for clientid in clients:
            await self._call("DELETE", f"clients/{quote(clientid, safe='')}", ok=(204, 404))
        return len(clients)

    async def publish_retained(self, topic: str, payload: bytes) -> None:
        # Published by the API, which no client authorization rule applies to.
        await self._call(
            "POST",
            "publish",
            {
                "topic": topic,
                "payload": payload.decode(),
                "payload_encoding": "plain",
                "qos": 1,
                "retain": True,
            },
            # 202 means no client is subscribed now. The message is still retained.
            ok=(200, 202),
        )
