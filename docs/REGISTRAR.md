# rf-registrar

`rf-registrar` makes and removes the broker logins of RF stations. The
community Discord bot calls it, through the `rfstations` cog. Members
manage their own station there. No operator has to touch the broker.

## Why a separate service

Making a login needs the broker's admin API. That key can change every
login and every rule on the broker, including the mesh ones. The bot
runs on another host and should never hold it. The registrar holds it,
runs beside the broker, and offers the bot only these operations, all
under `rf/`.

## Words

- A **location** is one station on the map. Its id is the station id,
  `[a-z0-9-]{3,32}`. It has a display name and an owner, a Discord
  member.
- A **source** is one Pi or decoder at a location. Its name is
  `[a-z0-9][a-z0-9-]{0,15}`. Each source has its own broker login,
  `rf-<location>-<source>`, and the agent there sets `SOURCE_ID` to the
  source name.

## What a login may do

The first matching rule decides:

| Topic | Publish |
|---|---|
| `rf/v1/<location>/registry` | denied |
| `rf/v1/<location>/status/<its own source>` | allowed |
| `rf/v1/<location>/status/#` (any other heartbeat, and `status`) | denied |
| `rf/v1/<location>/<kind>` | allowed |
| anything else, and every subscribe | denied |

The registrar publishes each location's registry entry, retained, to
`rf/v1/<location>/registry` through the admin API. It holds the display
name, the owner's name and the sources. Sonoran stores it and shows it
on the RF tab. It never holds a Discord id.

## Rules

- A password is shown once: in the answer to a mint or a rotation. The
  bot sends it to the owner in a direct message. The broker keeps only
  a hash. The registrar keeps nothing.
- Only the owner or an operator may change a location. The bot decides
  who is an operator, from a Discord role.
- A revocation deletes the login and then disconnects the Pi, so it
  cannot reconnect. A rotation changes the password and disconnects
  the Pi.
- A member may own `MAX_LOCATIONS_PER_OWNER` locations, with up to
  `MAX_SOURCES_PER_LOCATION` sources each, and may mint or rotate
  `MINTS_PER_HOUR` times an hour. An operator is not limited.
- A location in `RESERVED_LOCATIONS` can be created by an operator only.
  Use it for a station whose login was made by hand.
- Every change is logged in the registrar's database, with the actor.

## The broker

EMQX 5. The registrar needs:

- An API key and secret.
- The built-in user database as an authenticator.
- The built-in database as an **authorization source, before any file
  source**. A login's own rules then decide before a file rule does.
  A file rule such as `{deny, all, all, ["rf/v1/#"]}` still stops every
  other login.

## The API

Every request but `/v1/health` carries
`Authorization: Bearer <REGISTRAR_API_KEY>`. A change carries the actor:

```json
{"actor": {"id": "<discord user id>", "name": "Their name", "operator": false}}
```

| Request | Body | Answer |
|---|---|---|
| `GET /v1/health` | | `{"ok": true}` |
| `GET /v1/locations[?owner=<id>]` | | the locations, with their sources |
| `GET /v1/locations/<id>` | | one location |
| `POST /v1/locations` | `id`, `display_name` | the location, 201 |
| `POST /v1/locations/<id>/rename` | `display_name` | the location |
| `POST /v1/locations/<id>/transfer` | `owner_id`, `owner_name` | the location |
| `POST /v1/locations/<id>/delete` | | every source is revoked first |
| `POST /v1/locations/<id>/sources` | `source` | the credentials, 201 |
| `POST /v1/locations/<id>/sources/<s>/rotate` | | the credentials |
| `POST /v1/locations/<id>/sources/<s>/revoke` | | |

Errors are `{"error": "..."}` with 401, 403, 404, 409, 422, 429 or 502.

## Settings

| Variable | Meaning | Default |
|---|---|:-:|
| `REGISTRAR_API_KEY` | The bot's key, at least 32 characters. | — |
| `EMQX_API_URL` | Such as `http://emqx:18083/api/v5`. | — |
| `EMQX_API_KEY`, `EMQX_API_SECRET` | The broker's API key. | — |
| `DB_PATH` | The registrar's SQLite file. | `/data/registrar.sqlite3` |
| `LISTEN_HOST`, `LISTEN_PORT` | Where it listens. | `0.0.0.0`, `8080` |
| `MAX_LOCATIONS_PER_OWNER` | | `3` |
| `MAX_SOURCES_PER_LOCATION` | | `8` |
| `MINTS_PER_HOUR` | Mints and rotations per member. | `10` |
| `RESERVED_LOCATIONS` | Location ids, comma separated. | empty |
| `LOG_LEVEL` | | `INFO` |

Put it behind a reverse proxy with TLS, and do not publish its port
anywhere else. At start it publishes every registry entry again, in
case one was lost.
