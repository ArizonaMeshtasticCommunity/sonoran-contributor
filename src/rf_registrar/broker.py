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
ALL_RULES = "authorization/sources/built_in_database/rules/all"

# For every client the registrar's own rules do not name, such as a
# login made by hand whose file rule allows `rf/v1/<station>/#`. Without
# it, that login could publish its own registry entry. The registrar
# publishes through the API, which no client rule applies to.
REGISTRY_GUARD = {"permission": "deny", "action": "publish", "topic": "rf/v1/+/registry"}

# Sonoran's mesh jobs (Sonoran #178). Each login gets its own subtree in
# `rules_for`; the dispatcher gets `dispatcher_rules`. The broker allows
# what no rule names, so every other client is kept out by this. A
# login's own rules are checked before the all-clients rules: that is
# why a login made by hand bypasses REGISTRY_GUARD, and needs its own
# registry deny (docs/REGISTRAR.md).
CONTRIB_ROOT = "sonoran/v1/contributors"
CONTRIB_GUARD = {"permission": "deny", "action": "all", "topic": f"{CONTRIB_ROOT}/#"}
ALL_CLIENT_GUARDS = [REGISTRY_GUARD, CONTRIB_GUARD]


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
    async def guard_registry(self) -> bool: ...


def rules_for(station: str, source: str) -> list[dict[str, str]]:
    """The rules of one source's login, in order. The first match decides.

    The login may publish its own heartbeat and the station's data
    topics. It may not publish another source's heartbeat, the legacy
    heartbeat, or the registry. It subscribes to nothing. `status/#`
    also matches `status` itself.
    """
    base = f"rf/v1/{station}"
    own = f"{CONTRIB_ROOT}/{station}/{source}"
    return [
        {"permission": "deny", "action": "publish", "topic": f"{base}/registry"},
        {"permission": "allow", "action": "publish", "topic": f"{base}/status/{source}"},
        {"permission": "deny", "action": "publish", "topic": f"{base}/status/#"},
        {"permission": "allow", "action": "publish", "topic": f"{base}/+"},
        # Mesh jobs: read its own jobs, answer them, send its heartbeat.
        {"permission": "allow", "action": "subscribe", "topic": f"{own}/outbox/+"},
        {"permission": "allow", "action": "publish", "topic": f"{own}/inbox/+"},
        {"permission": "allow", "action": "publish", "topic": f"{own}/status"},
        {"permission": "deny", "action": "all", "topic": "#"},
    ]


def dispatcher_rules() -> list[dict[str, str]]:
    """The rules of Sonoran's dispatcher login, `sonoran-contrib`.

    The only login that may publish a job. It reads every result and
    heartbeat, and nothing outside the contributor tree.
    """
    return [
        {"permission": "allow", "action": "publish", "topic": f"{CONTRIB_ROOT}/+/+/outbox/+"},
        {"permission": "allow", "action": "subscribe", "topic": f"{CONTRIB_ROOT}/+/+/inbox/+"},
        {"permission": "allow", "action": "subscribe", "topic": f"{CONTRIB_ROOT}/+/+/status"},
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

    async def guard_registry(self) -> bool:
        """Make sure the all-clients guards are there: the registry, and the
        contributor tree (Sonoran #178).

        Returns True when one had to be added. The other all-client rules
        are kept as they are.
        """
        found = await self._call("GET", ALL_RULES)
        rules = list((found or {}).get("rules", []))
        missing = [
            guard
            for guard in ALL_CLIENT_GUARDS
            if not any(
                r.get("topic") == guard["topic"]
                and r.get("permission") == "deny"
                and r.get("action") in (guard["action"], "all")
                for r in rules
            )
        ]
        if not missing:
            return False
        await self._call("POST", ALL_RULES, {"rules": [*missing, *rules]})
        return True

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
