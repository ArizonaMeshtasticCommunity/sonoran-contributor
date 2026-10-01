"""The registrar's own records, in SQLite.

Who owns which location, which sources it has, and a log of every
change. Passwords are never stored: the broker keeps only a hash, and
the registrar keeps nothing.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS location (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source (
    location TEXT NOT NULL REFERENCES location(id),
    label TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    rotated_at REAL,
    PRIMARY KEY (location, label)
);
CREATE TABLE IF NOT EXISTS event (
    at REAL NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    location TEXT,
    label TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS event_actor_at ON event (actor_id, at);
"""


@dataclass(frozen=True)
class Location:
    id: str
    display_name: str
    owner_id: str
    owner_name: str
    created_at: float


@dataclass(frozen=True)
class Source:
    location: str
    label: str
    username: str
    created_at: float
    rotated_at: float | None


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def location(self, location_id: str) -> Location | None:
        row = self.db.execute(
            "SELECT id, display_name, owner_id, owner_name, created_at FROM location WHERE id = ?",
            (location_id,),
        ).fetchone()
        return Location(*row) if row else None

    def locations(self, owner_id: str | None = None) -> list[Location]:
        sql = "SELECT id, display_name, owner_id, owner_name, created_at FROM location"
        args: tuple[str, ...] = ()
        if owner_id is not None:
            sql += " WHERE owner_id = ?"
            args = (owner_id,)
        return [Location(*row) for row in self.db.execute(sql + " ORDER BY id", args)]

    def add_location(self, location: Location) -> None:
        self.db.execute(
            "INSERT INTO location VALUES (?, ?, ?, ?, ?)",
            (
                location.id,
                location.display_name,
                location.owner_id,
                location.owner_name,
                location.created_at,
            ),
        )

    def update_location(self, location_id: str, **values: str) -> None:
        for column, value in values.items():
            if column not in ("display_name", "owner_id", "owner_name"):
                raise ValueError(column)
            self.db.execute(f"UPDATE location SET {column} = ? WHERE id = ?", (value, location_id))

    def remove_location(self, location_id: str) -> None:
        self.db.execute("DELETE FROM location WHERE id = ?", (location_id,))

    def sources(self, location_id: str) -> list[Source]:
        return [
            Source(*row)
            for row in self.db.execute(
                "SELECT location, label, username, created_at, rotated_at FROM source "
                "WHERE location = ? ORDER BY label",
                (location_id,),
            )
        ]

    def source(self, location_id: str, label: str) -> Source | None:
        row = self.db.execute(
            "SELECT location, label, username, created_at, rotated_at FROM source "
            "WHERE location = ? AND label = ?",
            (location_id, label),
        ).fetchone()
        return Source(*row) if row else None

    def add_source(self, source: Source) -> None:
        self.db.execute(
            "INSERT INTO source VALUES (?, ?, ?, ?, ?)",
            (source.location, source.label, source.username, source.created_at, source.rotated_at),
        )

    def rotated(self, location_id: str, label: str, at: float) -> None:
        self.db.execute(
            "UPDATE source SET rotated_at = ? WHERE location = ? AND label = ?",
            (at, location_id, label),
        )

    def remove_source(self, location_id: str, label: str) -> None:
        self.db.execute("DELETE FROM source WHERE location = ? AND label = ?", (location_id, label))

    def log(
        self,
        actor_id: str,
        action: str,
        location: str | None = None,
        label: str | None = None,
        detail: str | None = None,
        at: float | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO event VALUES (?, ?, ?, ?, ?, ?)",
            (at if at is not None else time.time(), actor_id, action, location, label, detail),
        )

    def count_since(self, actor_id: str, actions: tuple[str, ...], since: float) -> int:
        marks = ", ".join("?" for _ in actions)
        (count,) = self.db.execute(
            f"SELECT count(*) FROM event WHERE actor_id = ? AND action IN ({marks}) AND at >= ?",
            (actor_id, *actions, since),
        ).fetchone()
        return int(count)
