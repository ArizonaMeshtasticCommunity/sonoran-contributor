"""The contributor's settings, from environment variables only.

The broker and the station are the RF agent's own settings (README), so
one Pi has one login and one set of variables for both. Mesh jobs need
`SOURCE_ID`, because the topics name the source, and `MESH_NODE`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from rf_agent.config import MQTTS_PORT
from rf_agent.messages import SOURCE_ID, STATION_ID
from sonoran_contributor.contract import KNOWN_KINDS
from sonoran_contributor.node import DEFAULT_TCP_PORT


class ConfigError(ValueError):
    """The settings cannot work. The message lists every problem."""


@dataclass(frozen=True)
class MeshConfig:
    location: str
    source: str
    broker_host: str
    broker_port: int
    broker_user: str
    broker_pass: str
    # Exactly one of these is set.
    node_host: str | None
    node_port: int
    node_serial: str | None
    allow: frozenset[str]
    log_level: str


def mesh_enabled(env: Mapping[str, str]) -> bool:
    """Mesh jobs run only when the member names a node."""
    return bool(env.get("MESH_NODE", "").strip())


def load_mesh(env: Mapping[str, str]) -> MeshConfig:
    errors: list[str] = []
    location = env.get("STATION_ID", "").strip()
    if not STATION_ID.match(location):
        errors.append("STATION_ID must match [a-z0-9-]{3,32}")
    source = env.get("SOURCE_ID", "").strip().lower()
    if not SOURCE_ID.match(source):
        errors.append("SOURCE_ID is required for mesh jobs and must match [a-z0-9][a-z0-9-]{0,15}")

    broker_url = env.get("BROKER_URL", "").strip()
    parts = urlsplit(broker_url)
    broker_host, broker_port = parts.hostname or "", MQTTS_PORT
    if parts.scheme != "mqtts" or not broker_host:
        errors.append("BROKER_URL must be mqtts://host[:port]; the contributor only talks TLS")
    else:
        try:
            broker_port = parts.port or MQTTS_PORT
        except ValueError:
            errors.append("BROKER_URL has a bad port")

    node_host = node_serial = None
    node_port = DEFAULT_TCP_PORT
    node = urlsplit(env.get("MESH_NODE", "").strip())
    if node.scheme == "tcp" and node.hostname:
        node_host = node.hostname
        try:
            node_port = node.port or DEFAULT_TCP_PORT
        except ValueError:
            errors.append("MESH_NODE has a bad port")
    elif node.scheme == "serial" and node.path:
        node_serial = node.path
    else:
        errors.append("MESH_NODE must be tcp://host[:4403] or serial:///dev/ttyUSB0")

    allow_text = env.get("MESH_JOBS", "local_stats")
    allow = frozenset(item.strip() for item in allow_text.split(",") if item.strip())
    unknown = allow - KNOWN_KINDS
    if unknown:
        errors.append(
            f"MESH_JOBS: unknown kind(s) {', '.join(sorted(unknown))}; "
            f"kinds are {', '.join(sorted(KNOWN_KINDS))}"
        )

    if errors:
        raise ConfigError("; ".join(errors))
    return MeshConfig(
        location=location,
        source=source,
        broker_host=broker_host,
        broker_port=broker_port,
        # The RF agent's default, so RF and mesh jobs use one login (audit F2).
        broker_user=env.get("BROKER_USER", "").strip() or location,
        broker_pass=env.get("BROKER_PASS", ""),
        node_host=node_host,
        node_port=node_port,
        node_serial=node_serial,
        allow=allow,
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )
