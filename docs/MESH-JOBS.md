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

## The node

`MESH_NODE=tcp://host[:port]` uses the node's TCP API, port 4403 by
default (`DEFAULT_TCP_PORT`, `meshtastic/tcp_interface.py`).
`MESH_NODE=serial:///dev/ttyUSB0` uses a USB serial port; pass the device
to the container. A node in use by another TCP client, such as a phone
app over the network, may refuse a second connection: give the
contributor its own way in.
