# Mesh jobs

Sonoran's design is `docs/design/contributors.md` in beta-sonoran. This
page is the contributor's side.

## Topics

Under `sonoran/v1/contributors/<location>/<source>/`:

| Topic | Direction | Retained | |
|---|---|:-:|---|
| `outbox/<job>` | Sonoran to us | yes | A job. Empty: the job is cleared. |
| `inbox/<job>` | us to Sonoran | no | The result, QoS 1. |
| `status` | us to Sonoran | yes | The heartbeat, every 60 s. |

The broker lets this login subscribe to its own `outbox` only, and
publish to its own `inbox` and `status` only. The registrar writes
those rules.

## A job

1. A retained job arrives again after every reconnect. `Seen` makes sure
   it is done once.
2. A job whose `expires` has passed is ignored. Sonoran has given it to
   someone else.
3. A kind not in `MESH_JOBS` is answered `refused`, and the node is not
   used.
4. The node asks the target and waits at most 300 seconds, and never past
   the job's `expires`. 300 seconds is `MeshInterface(timeout=300)` in the
   Meshtastic Python library (`meshtastic/mesh_interface.py` line 93,
   2.7.11).
5. The result is `ok` with the LocalStats fields by their protobuf
   names, `no_reply`, or `error` with the routing error, such as
   `NO_RESPONSE`.

A noise floor of 0 is "not measured": the firmware leaves it 0 when the
radio cannot measure it (`getLocalStatsTelemetry`,
`src/modules/Telemetry/DeviceTelemetry.cpp`, meshtastic/firmware
v2.7.26.54e0d8d). It is left out of the result.

## The heartbeat

It lists up to 250 nodes from the node's own node list, the most
recently heard first, with their hops. 250 is `MAX_NUM_NODES` on an
ESP32-S3 with 16 MB flash (`src/mesh/mesh-pb-constants.h`,
meshtastic/firmware), the largest list a radio keeps. Sonoran uses it
to choose the contributor with the fewest hops to a target.

## Who decides what

**You decide, on your Pi:**

- Whether it does RF (`SOURCES`), mesh jobs (`MESH_NODE`), or both.
- Which node it uses (`MESH_NODE`).
- Which job kinds it does (`MESH_JOBS`, `local_stats` by default). The
  heartbeat tells Sonoran this list, and Sonoran sends no other kind. A
  job of another kind is answered `refused`, and your node is not used.
- When it stops: stop the container, or remove `MESH_NODE`.

**Sonoran decides:** which node your Pi is asked to read, and when. It
chooses from the nodes your node hears, at most once per round for each
node (12 hours for a router, 1 hour for other roles), and one job at a
time for your Pi.

**You cannot** choose the nodes or make a job: your login cannot publish
to an outbox. **Sonoran cannot** start RF on your Pi, send a kind you
did not allow, or have your node do anything but the one request.

**The operator** can stop all jobs (the `contrib` service in Sonoran),
or revoke your login, which stops RF and jobs together. There is no
switch yet to pause only your jobs.

## Set up your node

You need a Meshtastic node that is on the mesh and that the contributor
can reach. It can be a radio on your network, a radio on USB, or
`meshtasticd` on Linux.

### 1. The node

- It is on the same modem preset as the routers it should read, and it
  hears some of them. It does not need to be a router.
- Its node list shows the routers it hears. The contributor sends that
  list to Sonoran every minute, and Sonoran gives jobs only for nodes in
  it.
- The job is checked against firmware v2.7.26
  (`DeviceTelemetryModule::allocReply`). An older firmware on the node or
  on the target may not answer.

### 2. How the contributor reaches it

**Over the network (TCP).** A radio with WiFi or Ethernet accepts TCP
connections on port 4403 (Meshtastic docs, "Connections", TCP/IP; and
`DEFAULT_TCP_PORT` in the Python library). Put the radio on your network
with its own WiFi or Ethernet settings, then give it a fixed address.

```yaml
services:
  sonoran-contributor:
    image: ghcr.io/arizonameshtasticcommunity/sonoran-contributor:main
    restart: unless-stopped
    environment:
      STATION_ID: tempe-roof
      SOURCE_ID: mesh-pi
      BROKER_URL: mqtts://broker.example:8883
      BROKER_USER: rf-tempe-roof-mesh-pi
      BROKER_PASS: ${CONTRIBUTOR_PASS}
      MESH_NODE: tcp://192.168.1.50      # your node; :4403 is the default
```

A node that a phone app already uses over the network may refuse a
second TCP client. Give the contributor its own way in, or use USB.

**Over USB (serial).** Pass the device into the container, and let the
container's user open it. The device belongs to the group `dialout`, and
the container's user is not in it, so add the group:

```yaml
    devices:
      - /dev/ttyUSB0:/dev/ttyUSB0       # or /dev/ttyACM0; see `ls /dev/tty*`
    group_add:
      - dialout                          # or the number from `stat -c %g /dev/ttyUSB0`
    environment:
      # ...as above...
      MESH_NODE: serial:///dev/ttyUSB0
```

Without `group_add` the log shows `Meshtastic node: ...` with
"Permission denied".

**`meshtasticd` on Linux** serves the same API on port 4403 (Meshtastic
docs, "meshtasticd usage"). Use `MESH_NODE: tcp://<host>`.

### 3. The login

Use the login the Discord bot minted for this Pi:
`/contributor token mint <location> <source>`. One login does RF and mesh
jobs. **RF is not needed:** a Pi with mesh jobs only sets no
`SOURCES`, no station position, and runs no SDR or `rf-sweep`. It needs
`STATION_ID`, `SOURCE_ID`, `BROKER_URL`, `BROKER_USER`, `BROKER_PASS`
and `MESH_NODE`.

### 4. Check that it works

`docker compose logs -f sonoran-contributor` shows:

1. `mesh jobs: connected to node !a1b2c3d4, 57 nodes in its list` at
   start. The number is the node's own list.
2. Nothing more while there is no job. The heartbeat is not logged.
3. `job <id> for !1234abcd: ok` when a job is done, or `no_reply`,
   `error` or `refused`.

A job comes only when a router that your node hears is due: at most once
in 12 hours for a router, 1 hour for other roles. The first job can take
a while. In Sonoran, the router's node page then shows **Local stats**,
"read by" your location.

If it fails, the log says so once, when it starts, and again each hour
while it lasts:

| Log | What to check |
|---|---|
| `Meshtastic node: ... Permission denied` | `group_add` (USB) |
| `Meshtastic node: ... Connection refused`, or a timeout | The address, the port, and that the radio's network is on |
| `broker: ...` | `BROKER_URL`, `BROKER_USER`, `BROKER_PASS` |
| `job ...: no_reply` again and again | The target does not answer your node. Sonoran gives the job to the next contributor. |

## When something restarts

- **Your node** goes away (rebooted, unplugged, off the network): the job
  that was running ends in `error`, with the reason, and Sonoran gives it
  to someone else. The next job opens a new connection. RF keeps
  running. The Meshtastic library also reconnects a TCP connection on
  its own.
- **The broker**: the contributor reconnects with backoff, up to a minute
  apart. Jobs that expired meanwhile are ignored.
- **Sonoran**: nothing to do. A job that expires is given again.

## Upgrading from `rf-agent`

The image is the same. Change `rf-agent` to `sonoran-contributor` in the
image name, and keep your settings. RF runs as before. Add `MESH_NODE`
only if you want mesh jobs. The `rf-agent` image name and command still
work.

## The node, in detail


`MESH_NODE=tcp://host[:port]` uses the node's TCP API, port 4403 by
default (`DEFAULT_TCP_PORT`, `meshtastic/tcp_interface.py`).
`MESH_NODE=serial:///dev/ttyUSB0` uses a USB serial port; pass the device
to the container. A node in use by another TCP client, such as a phone
app over the network, may refuse a second connection: give the
contributor its own way in.
