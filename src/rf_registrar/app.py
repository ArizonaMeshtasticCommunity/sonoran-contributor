"""The registrar's HTTP API, for the Discord bot only.

Every request carries `Authorization: Bearer <REGISTRAR_API_KEY>`. A
request that changes something carries the actor in its JSON body:

    {"actor": {"id": "<discord user id>", "name": "...", "operator": false}}

    GET  /v1/health
    GET  /v1/locations[?owner=<id>]
    GET  /v1/locations/<id>
    POST /v1/locations                         {id, display_name}
    POST /v1/locations/<id>/rename             {display_name}
    POST /v1/locations/<id>/transfer           {owner_id, owner_name}
    POST /v1/locations/<id>/delete
    POST /v1/locations/<id>/sources            {source}  -> credentials
    POST /v1/locations/<id>/sources/<s>/rotate           -> credentials
    POST /v1/locations/<id>/sources/<s>/revoke

Credentials are in the answer once and nowhere else.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import aiohttp
from aiohttp import web

from rf_registrar.broker import Emqx
from rf_registrar.config import Config, ConfigError, load
from rf_registrar.service import Actor, Invalid, Registrar, RegistrarError
from rf_registrar.store import Store

logger = logging.getLogger(__name__)

REGISTRAR = web.AppKey("registrar", Registrar)
CONFIG = web.AppKey("config", Config)
MAX_BODY = 16 * 1024

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def guard(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Check the key, and turn a registrar error into its status."""
    if request.path != "/v1/health":
        given = request.headers.get("Authorization", "")
        expected = f"Bearer {request.app[CONFIG].api_key}"
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
    try:
        return await handler(request)
    except RegistrarError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise Invalid("the body is not JSON") from exc
    if not isinstance(body, dict):
        raise Invalid("the body is not an object")
    return body


def _actor(body: dict[str, Any]) -> Actor:
    raw = body.get("actor")
    if not isinstance(raw, dict):
        raise Invalid("actor is required")
    ident, name = raw.get("id"), raw.get("name")
    if not isinstance(ident, str) or not ident.isdigit():
        raise Invalid("actor.id is a Discord user id")
    if not isinstance(name, str):
        raise Invalid("actor.name is required")
    return Actor(ident, name, raw.get("operator") is True)


routes = web.RouteTableDef()


@routes.get("/v1/health")
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@routes.get("/v1/locations")
async def list_locations(request: web.Request) -> web.Response:
    registrar = request.app[REGISTRAR]
    owner = request.query.get("owner")
    return web.json_response([registrar.describe(loc) for loc in registrar.list(owner)])


@routes.get("/v1/locations/{id}")
async def get_location(request: web.Request) -> web.Response:
    registrar = request.app[REGISTRAR]
    return web.json_response(registrar.describe(registrar.get(request.match_info["id"])))


@routes.post("/v1/locations")
async def create(request: web.Request) -> web.Response:
    registrar = request.app[REGISTRAR]
    body = await _body(request)
    location = await registrar.create(body.get("id"), body.get("display_name"), _actor(body))
    return web.json_response(registrar.describe(location), status=201)


@routes.post("/v1/locations/{id}/rename")
async def rename(request: web.Request) -> web.Response:
    registrar = request.app[REGISTRAR]
    body = await _body(request)
    location = await registrar.rename(
        request.match_info["id"], body.get("display_name"), _actor(body)
    )
    return web.json_response(registrar.describe(location))


@routes.post("/v1/locations/{id}/transfer")
async def transfer(request: web.Request) -> web.Response:
    registrar = request.app[REGISTRAR]
    body = await _body(request)
    location = await registrar.transfer(
        request.match_info["id"], body.get("owner_id"), body.get("owner_name"), _actor(body)
    )
    return web.json_response(registrar.describe(location))


@routes.post("/v1/locations/{id}/delete")
async def delete(request: web.Request) -> web.Response:
    body = await _body(request)
    await request.app[REGISTRAR].delete(request.match_info["id"], _actor(body))
    return web.json_response({"deleted": request.match_info["id"]})


@routes.post("/v1/locations/{id}/sources")
async def mint(request: web.Request) -> web.Response:
    body = await _body(request)
    creds = await request.app[REGISTRAR].mint(
        request.match_info["id"], body.get("source"), _actor(body)
    )
    return web.json_response(asdict(creds), status=201)


@routes.post("/v1/locations/{id}/sources/{source}/rotate")
async def rotate(request: web.Request) -> web.Response:
    body = await _body(request)
    creds = await request.app[REGISTRAR].rotate(
        request.match_info["id"], request.match_info["source"], _actor(body)
    )
    return web.json_response(asdict(creds))


@routes.post("/v1/locations/{id}/sources/{source}/revoke")
async def revoke(request: web.Request) -> web.Response:
    body = await _body(request)
    await request.app[REGISTRAR].revoke(
        request.match_info["id"], request.match_info["source"], _actor(body)
    )
    return web.json_response({"revoked": request.match_info["source"]})


def make_app(config: Config, registrar: Registrar) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[CONFIG] = config
    app[REGISTRAR] = registrar
    app.add_routes(routes)
    return app


async def serve(config: Config) -> None:
    async with aiohttp.ClientSession() as session:
        broker = Emqx(config.emqx_url, config.emqx_key, config.emqx_secret, session)
        registrar = Registrar(config, Store(config.db_path), broker)
        # Put every registry entry right, in case a publish was lost.
        await registrar.republish_all()
        runner = web.AppRunner(make_app(config, registrar), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, config.host, config.port).start()
        logger.info("rf-registrar listening on %s:%d", config.host, config.port)
        await asyncio.Event().wait()


def run() -> None:
    """Console script entry point."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load(os.environ)
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    asyncio.run(serve(config))
