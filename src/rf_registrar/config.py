"""Configuration, from environment variables only.

A setting that is wrong stops the registrar at start, with every
problem named at once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rf_agent.messages import STATION_ID

# The bot's key must be long enough that guessing it is not a plan.
MIN_KEY_CHARS = 32


class ConfigError(ValueError):
    """One or more settings are missing or wrong."""


@dataclass(frozen=True)
class Config:
    api_key: str
    emqx_url: str
    emqx_key: str
    emqx_secret: str
    db_path: str = "/data/registrar.sqlite3"
    host: str = "0.0.0.0"
    port: int = 8080
    # Limits for a member. An operator is not limited.
    max_locations: int = 3
    max_sources: int = 8
    mints_per_hour: int = 10
    # Location ids only an operator may create, such as a station whose
    # login was made by hand before the registrar.
    reserved: frozenset[str] = frozenset()
    # Sonoran's dispatcher login (Sonoran #178), made by an operator. At
    # start the registrar writes its rules: the only login that may
    # publish a mesh job. Empty: no dispatcher rules are written.
    dispatcher_username: str = ""


def _int(env: Mapping[str, str], name: str, default: int, errors: list[str]) -> int:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        errors.append(f"{name} must be a whole number")
        return default
    if value < 0:
        errors.append(f"{name} must not be negative")
    return value


def load(env: Mapping[str, str]) -> Config:
    """Read the configuration. Raises ConfigError naming every problem."""
    errors: list[str] = []
    api_key = env.get("REGISTRAR_API_KEY", "")
    if len(api_key) < MIN_KEY_CHARS:
        errors.append(f"REGISTRAR_API_KEY must be at least {MIN_KEY_CHARS} characters")
    emqx_url = env.get("EMQX_API_URL", "").strip().rstrip("/")
    if not emqx_url.startswith(("http://", "https://")):
        errors.append("EMQX_API_URL is required, such as http://emqx:18083/api/v5")
    emqx_key = env.get("EMQX_API_KEY", "").strip()
    emqx_secret = env.get("EMQX_API_SECRET", "")
    if not emqx_key or not emqx_secret:
        errors.append("EMQX_API_KEY and EMQX_API_SECRET are required")
    listed = env.get("RESERVED_LOCATIONS", "").split(",")
    reserved = frozenset(item.strip().lower() for item in listed if item.strip())
    bad = sorted(item for item in reserved if not STATION_ID.match(item))
    if bad:
        errors.append(f"RESERVED_LOCATIONS holds ids that are not station ids: {', '.join(bad)}")
    config = Config(
        api_key=api_key,
        emqx_url=emqx_url,
        emqx_key=emqx_key,
        emqx_secret=emqx_secret,
        db_path=env.get("DB_PATH", "").strip() or "/data/registrar.sqlite3",
        host=env.get("LISTEN_HOST", "").strip() or "0.0.0.0",
        port=_int(env, "LISTEN_PORT", 8080, errors),
        max_locations=_int(env, "MAX_LOCATIONS_PER_OWNER", 3, errors),
        max_sources=_int(env, "MAX_SOURCES_PER_LOCATION", 8, errors),
        mints_per_hour=_int(env, "MINTS_PER_HOUR", 10, errors),
        reserved=reserved,
        dispatcher_username=env.get("DISPATCHER_USERNAME", "").strip(),
    )
    if errors:
        raise ConfigError("; ".join(errors))
    return config
