"""What the registrar does: locations, their sources, and their logins.

A location is one station on the map. Its owner is a Discord member. A
source is one Pi or decoder at the location, with its own broker login,
`rf-<location>-<source>`. The bot says who is asking (`Actor`) and
whether they are an operator. The registrar checks that the actor owns
the location, unless they are an operator.

Every change is logged, and every change to a location or its sources
publishes the location's registry entry, retained, through the broker.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rf_agent.messages import SOURCE_ID, STATION_ID
from rf_registrar.broker import Broker, BrokerError, rules_for
from rf_registrar.config import Config
from rf_registrar.store import Location, Source, Store

logger = logging.getLogger(__name__)

MAX_NAME = 64
# Sonoran refuses a registry entry with more sources than this, so no
# location may have more, an operator's included.
HARD_MAX_SOURCES = 32
PASSWORD_BYTES = 24


class RegistrarError(Exception):
    status = 400


class Invalid(RegistrarError):
    status = 422


class NotFound(RegistrarError):
    status = 404


class Forbidden(RegistrarError):
    status = 403


class Conflict(RegistrarError):
    status = 409


class Limited(RegistrarError):
    status = 429


class Unavailable(RegistrarError):
    status = 502


@dataclass(frozen=True)
class Actor:
    """Who asks, as the bot saw them. `id` is a Discord user id."""

    id: str
    name: str
    operator: bool = False


@dataclass(frozen=True)
class Credentials:
    """A login, shown once. The registrar does not keep the password."""

    station: str
    source: str
    username: str
    password: str


def username_for(location: str, label: str) -> str:
    return f"rf-{location}-{label}"


def clean_name(text: object, what: str) -> str:
    """A display name: printable, trimmed, 1 to 64 characters."""
    if not isinstance(text, str):
        raise Invalid(f"{what} is required")
    name = " ".join("".join(ch for ch in text if unicodedata.category(ch)[0] != "C").split())
    if not 1 <= len(name) <= MAX_NAME:
        raise Invalid(f"{what} must be 1 to {MAX_NAME} characters")
    return name


def _location_id(value: object) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not STATION_ID.match(text):
        raise Invalid("a location id is 3 to 32 of a-z, 0-9 and -")
    return text


def _label(value: object) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not SOURCE_ID.match(text):
        raise Invalid("a source name is 1 to 16 of a-z, 0-9 and -, not starting with -")
    return text


class Registrar:
    def __init__(
        self,
        config: Config,
        store: Store,
        broker: Broker,
        clock: Callable[[], float] = time.time,
        password: Callable[[], str] = lambda: secrets.token_urlsafe(PASSWORD_BYTES),
    ) -> None:
        self.config = config
        self.store = store
        self.broker = broker
        self.clock = clock
        self.password = password

    # --- reading ------------------------------------------------------

    def describe(self, location: Location) -> dict[str, Any]:
        return {
            "id": location.id,
            "display_name": location.display_name,
            "owner_id": location.owner_id,
            "owner_name": location.owner_name,
            "created_at": location.created_at,
            "sources": [
                {
                    "source": s.label,
                    "username": s.username,
                    "created_at": s.created_at,
                    "rotated_at": s.rotated_at,
                }
                for s in self.store.sources(location.id)
            ],
        }

    def get(self, location_id: str) -> Location:
        location = self.store.location(_location_id(location_id))
        if location is None:
            raise NotFound(f"no location {location_id!r}")
        return location

    def owned(self, location_id: str, actor: Actor) -> Location:
        """The location, if the actor owns it or is an operator."""
        location = self.get(location_id)
        if not actor.operator and location.owner_id != actor.id:
            raise Forbidden(f"{location.id} belongs to someone else")
        return location

    def list(self, owner_id: str | None = None) -> list[Location]:
        return self.store.locations(owner_id)

    # --- the registry -------------------------------------------------

    def registry_entry(self, location: Location) -> dict[str, Any]:
        return {
            "v": 1,
            "station": location.id,
            "sent": datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "display_name": location.display_name,
            "owner": location.owner_name,
            "sources": [s.label for s in self.store.sources(location.id)],
        }

    async def publish(self, location_id: str) -> None:
        """Publish the location's registry entry, or clear it if it is gone.

        A failure is logged, not raised: the login change already took
        effect. `republish_all` at start puts every entry right again.
        """
        location = self.store.location(location_id)
        payload = b"" if location is None else json.dumps(self.registry_entry(location)).encode()
        try:
            await self.broker.publish_retained(f"rf/v1/{location_id}/registry", payload)
        except BrokerError:
            logger.exception("could not publish the registry entry of %s", location_id)

    async def start(self) -> None:
        """At start: guard the registry topic, then publish every entry again."""
        try:
            if await self.broker.guard_registry():
                logger.warning("added the broker rule that guards rf/v1/+/registry")
        except BrokerError:
            logger.exception(
                "could not check the broker rule that guards rf/v1/+/registry; "
                "a login made by hand may be able to write its registry entry"
            )
        await self.republish_all()

    async def republish_all(self) -> None:
        for location in self.store.locations():
            await self.publish(location.id)

    # --- locations ----------------------------------------------------

    async def create(self, location_id: object, display_name: object, actor: Actor) -> Location:
        ident = _location_id(location_id)
        name = clean_name(display_name, "display name")
        if ident in self.config.reserved and not actor.operator:
            raise Forbidden(f"{ident} is reserved; ask an operator")
        if self.store.location(ident) is not None:
            raise Conflict(f"{ident} is taken")
        if not actor.operator and len(self.store.locations(actor.id)) >= self.config.max_locations:
            raise Limited(f"you own {self.config.max_locations} locations already")
        owner = clean_name(actor.name, "owner name")
        location = Location(ident, name, actor.id, owner, self.clock())
        self.store.add_location(location)
        self.store.log(actor.id, "create", ident, detail=name, at=self.clock())
        await self.publish(ident)
        return location

    async def rename(self, location_id: str, display_name: object, actor: Actor) -> Location:
        location = self.owned(location_id, actor)
        name = clean_name(display_name, "display name")
        self.store.update_location(location.id, display_name=name)
        self.store.log(actor.id, "rename", location.id, detail=name, at=self.clock())
        await self.publish(location.id)
        return self.get(location.id)

    async def transfer(
        self, location_id: str, new_owner_id: object, new_owner_name: object, actor: Actor
    ) -> Location:
        location = self.owned(location_id, actor)
        if not isinstance(new_owner_id, str) or not new_owner_id.isdigit():
            raise Invalid("the new owner is a Discord user id")
        name = clean_name(new_owner_name, "new owner name")
        owned = len(self.store.locations(new_owner_id))
        if not actor.operator and owned >= self.config.max_locations:
            raise Limited(f"the new owner has {self.config.max_locations} locations already")
        self.store.update_location(location.id, owner_id=new_owner_id, owner_name=name)
        self.store.log(actor.id, "transfer", location.id, detail=new_owner_id, at=self.clock())
        await self.publish(location.id)
        return self.get(location.id)

    async def delete(self, location_id: str, actor: Actor) -> None:
        """Revoke every source, then remove the location and its entry."""
        location = self.owned(location_id, actor)
        for source in self.store.sources(location.id):
            await self._remove_login(source)
            self.store.remove_source(location.id, source.label)
            self.store.log(actor.id, "revoke", location.id, source.label, at=self.clock())
        self.store.remove_location(location.id)
        self.store.log(actor.id, "delete", location.id, at=self.clock())
        await self.publish(location.id)

    # --- sources ------------------------------------------------------

    def _check_rate(self, actor: Actor) -> None:
        if actor.operator:
            return
        used = self.store.count_since(actor.id, ("mint", "rotate"), self.clock() - 3600)
        if used >= self.config.mints_per_hour:
            raise Limited(f"at most {self.config.mints_per_hour} mints and rotations an hour")

    async def mint(self, location_id: str, label: object, actor: Actor) -> Credentials:
        location = self.owned(location_id, actor)
        name = _label(label)
        if self.store.source(location.id, name) is not None:
            raise Conflict(f"{location.id} has a source {name} already; rotate it instead")
        count = len(self.store.sources(location.id))
        if count >= HARD_MAX_SOURCES or (not actor.operator and count >= self.config.max_sources):
            limit = HARD_MAX_SOURCES if actor.operator else self.config.max_sources
            raise Limited(f"a location has at most {limit} sources")
        self._check_rate(actor)
        username = username_for(location.id, name)
        password = self.password()
        try:
            await self.broker.create_user(username, password)
        except BrokerError as exc:
            raise Unavailable(f"the broker refused the login: {exc}") from exc
        try:
            await self.broker.set_rules(username, rules_for(location.id, name))
        except BrokerError as exc:
            # A login without its rules would fall through to the
            # broker's defaults. Take it away again.
            await self._quietly(self.broker.delete_user(username))
            raise Unavailable(f"the broker refused the rules: {exc}") from exc
        self.store.add_source(Source(location.id, name, username, self.clock(), None))
        self.store.log(actor.id, "mint", location.id, name, at=self.clock())
        await self.publish(location.id)
        return Credentials(location.id, name, username, password)

    async def rotate(self, location_id: str, label: object, actor: Actor) -> Credentials:
        """A new password. The Pi is disconnected and the old one stops working."""
        location = self.owned(location_id, actor)
        source = self._source(location.id, label)
        self._check_rate(actor)
        password = self.password()
        try:
            await self.broker.set_password(source.username, password)
            await self.broker.kick(source.username)
        except BrokerError as exc:
            raise Unavailable(f"the broker refused: {exc}") from exc
        self.store.rotated(location.id, source.label, self.clock())
        self.store.log(actor.id, "rotate", location.id, source.label, at=self.clock())
        return Credentials(location.id, source.label, source.username, password)

    async def revoke(self, location_id: str, label: object, actor: Actor) -> None:
        """Remove the login. The Pi is disconnected at once."""
        location = self.owned(location_id, actor)
        source = self._source(location.id, label)
        await self._remove_login(source)
        self.store.remove_source(location.id, source.label)
        self.store.log(actor.id, "revoke", location.id, source.label, at=self.clock())
        await self.publish(location.id)

    def _source(self, location_id: str, label: object) -> Source:
        name = _label(label)
        source = self.store.source(location_id, name)
        if source is None:
            raise NotFound(f"{location_id} has no source {name}")
        return source

    async def _remove_login(self, source: Source) -> None:
        # The login first, so the Pi cannot reconnect after the kick.
        try:
            await self.broker.delete_user(source.username)
            await self.broker.kick(source.username)
            await self.broker.delete_rules(source.username)
        except BrokerError as exc:
            raise Unavailable(f"the broker refused: {exc}") from exc

    @staticmethod
    async def _quietly(call: Any) -> None:
        try:
            await call
        except BrokerError:
            logger.exception("cleanup failed")
