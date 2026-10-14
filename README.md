<div align="center">

# sonoran-contributor

**What the community's own radios hear, and what our own nodes can ask.**
Local receivers · mesh jobs on your own node · MQTT to one broker · stored and mapped by Sonoran.

[![Contract](https://img.shields.io/badge/contract-v1-EA6A3A?style=flat-square)](schema/rf-message-v1.schema.json)
[![Phase](https://img.shields.io/badge/phases-1--5%20built-2A6DB2?style=flat-square)](#-roadmap)
[![CI](https://github.com/ArizonaMeshtasticCommunity/rf-map/actions/workflows/ci.yml/badge.svg)](https://github.com/ArizonaMeshtasticCommunity/rf-map/actions/workflows/ci.yml)
[![Sources](https://img.shields.io/badge/sources-local%20only-3776AB?style=flat-square)](#-what-it-reads)
[![Sonoran](https://img.shields.io/badge/stored%20by-Sonoran-EA6A3A?style=flat-square)](https://github.com/ArizonaMeshtasticCommunity/beta-sonoran)

</div>

---

`sonoran-contributor` runs at a member's site. It does two things, and
you turn on either or both:

- **RF**: the RF agent (`rf-agent`, below) reads the decoders beside
  your SDR.
- **Mesh jobs**: it asks nodes on the mesh for what they do not
  broadcast, with your own Meshtastic node, for Sonoran. The first job
  is the measured noise floor. See [Mesh jobs](#-mesh-jobs-for-sonoran).

`rf-agent` runs beside a member's SDR. It reads the decoder they already
run, thins what it hears, and publishes it to the community broker.
Sonoran stores it and draws it on its **RF** tab: aircraft first, then
APRS, radiosondes, weather sensors, and the 902 to 928 MHz band the mesh
lives in.

Every point comes from a receiver one of us runs. Nothing is fetched from
the internet: no aggregator, no APRS-IS, no SondeHub. An aircraft missing
from the map was not received by any of our stations. It is not air
traffic control and not for navigation.

## ✨ Features

- 📶 **Spectrum** — a spare dongle sweeps 902–928 MHz, the band the mesh lives in, to find what interferes with it, or any range you choose, or several in turn.
- 🧩 **Your own mods** — send what your own program decodes through the agent, as your station's data. See [docs/MODS.md](docs/MODS.md).
- ✈️ **Aircraft** — ADS-B at 1090 MHz and UAT at 978 MHz, from `readsb`, `dump1090-fa` or `dump978-fa`.
- 🤝 **Never takes your dongle** — reads the decoder's network port, so FlightAware, FR24 and every other feed keep running.
- 🪶 **Thinned at the edge** — one point per aircraft every 20 s leaves the site, not one a second.
- 📡 **Receiver reach** — each station's heartbeat says where it is and what it heard, so the map shows where the network hears, measured.
- 🔒 **Private by default** — station positions rounded before they leave; weather-sensor decoding is allow-list only, and empty until you name the models.
- 📜 **One contract** — [`schema/rf-message-v1.schema.json`](schema/rf-message-v1.schema.json), the same file Sonoran checks every message against.

## 🧩 How it fits together

```
member's site (one or more SDRs)                       Sonoran host
┌──────────────────────────────────────┐
│ readsb ──:30005──┐                   │
│ dump978 ─:30979──┤                   │   MQTT over TLS
│ direwolf ─:8001──┼──► rf-agent ──────┼──────────────► broker
│ rtl_433 ──UDP────┤   (read-only)     │                  │
│ auto_rx ──UDP────┘                   │                  ▼
└──────────────────────────────────────┘      sonoran-rf-ingest ──► RF tab
```

A USB SDR can be opened by one program at a time. `rf-agent` never opens
one: it connects to the output the decoder already serves, which takes
several readers at once.

## 📥 What it reads

| Source | Decoder you already run | What rf-agent reads | Band | Phase |
|--------|-------------------------|---------------------|:----:|:-----:|
| ADS-B | `readsb`, `dump1090-fa` | Beast on TCP 30005, or `aircraft.json` | 1090 MHz | **1** |
| UAT | `dump978-fa` | JSON on TCP 30979 | 978 MHz | **1** |
| Spectrum | `rtl_power` in `rf-sweep`, its own dongle | sweep lines on TCP 30950 | 902–928 MHz, or any range from 24 MHz to 1.766 GHz | **2** |
| APRS | `direwolf`, off the air | KISS on TCP 8001 | 144.39 MHz | **3** |
| Radiosondes | `radiosonde_auto_rx`, upload off | UDP 55672 | 400–406 MHz | **4** |
| Weather sensors | `rtl_433`, allow-listed | JSON over UDP | 433 / 915 MHz | **4** |
| Your own mod | your program | JSON lines over UDP 30960 | yours | **5** |

## 🚀 Quick start

Add one service beside whatever you already run. No device passthrough, no
privileges, no listening ports.

```yaml
services:
  rf-agent:
    image: ghcr.io/arizonameshtasticcommunity/sonoran-contributor:1
    restart: unless-stopped
    logging:
      options:
        max-size: "10m"
        max-file: "3"
    environment:
      STATION_ID: tempe-roof
      SOURCE_ID: adsb-pi
      BROKER_URL: mqtts://broker.example:8883
      BROKER_USER: rf-tempe-roof-adsb-pi
      BROKER_PASS: ${RF_AGENT_PASS}
      SOURCES: adsb=beast://readsb:30005,uat=tcp://dump978:30979
      STATION_LAT: "33.42"
      STATION_LON: "-111.94"
      STATION_PRECISION_M: "1000"
```

Get a login from the community Discord bot first:

1. `/rf location create` claims your station id. You become its owner.
2. `/rf token mint` makes one login for one Pi. The bot sends you
   `STATION_ID`, `SOURCE_ID`, `BROKER_USER` and `BROKER_PASS` in a direct
   message, once. Keep the password: it is never shown again.

A station with two Pis, for example one for ADS-B and one for the 900 MHz
sweep, is one station on the map. Mint one login for each Pi, each with
its own `SOURCE_ID`. You can rotate or revoke one Pi's login without
touching the other.

Each login may publish to its own station's topics and nothing else.

The agent logs a heartbeat line every minute: how many positions it
heard, sent and thinned. A decoder or broker that goes away is retried,
up to a minute apart; the agent never needs a restart for it. A setting
that is wrong stops it at start, with every problem named.

A failure that lasts is logged when it starts or changes, once an hour
while it continues, and once when it ends. It is not logged on every
retry, so an unplugged dongle or a decoder that is down for a week does
not fill the disk. The `logging:` lines above cap what Docker keeps, at
30 MB per container: Docker's default keeps everything. If your Docker
logs to journald or syslog instead, cap that there, such as
`SystemMaxUse=` in `journald.conf`.

> [!NOTE]
> dump978-fa serves raw frames on 30978 and decoded JSON on 30979. The
> agent reads the JSON. Pointed at the raw port, it logs that it is.

## 🛰️ Mesh jobs for Sonoran

A node measures its own radio, such as its noise floor, and keeps it in
`LocalStats`. It does not send that over the mesh. It answers a request
for it, to the node that asked, as a direct message. So Sonoran cannot
see it, but your node can ask (Sonoran #178).

Turn it on with `MESH_NODE`, beside the settings above. `SOURCE_ID` is
required. The same login does both RF and mesh jobs.

```yaml
    environment:
      # ...STATION_ID, SOURCE_ID, BROKER_* as above...
      MESH_NODE: tcp://192.168.1.50      # your node's TCP API, port 4403
      # MESH_NODE: serial:///dev/ttyUSB0 # or a USB serial port (add the device)
      # MESH_JOBS: local_stats           # the job kinds you allow; the default
```

How it works:

1. Every minute it sends Sonoran a heartbeat with the nodes your node
   hears, and how many hops away they are.
2. Sonoran picks a router that is due, and the contributor that hears it
   with the fewest hops. It puts one job in that contributor's outbox.
3. Your node sends that router one LocalStats request, and waits up to
   300 seconds for the answer.
4. The contributor sends the answer, or `no_reply`, to Sonoran. Sonoran
   shows it as the **measured noise floor** on the node page and the RF
   tab.

What it never does:

- It never sends an admin message, and never changes a setting on your
  node or any other.
- It does only the job kinds in `MESH_JOBS`.
- It does one job at a time. Sonoran asks a router no more often than
  the firmware's own telemetry interval: 12 hours for a router, 1 hour
  for other roles.
- It opens no port. It connects out to your node and to the broker.

The topics and messages are in
[`schema/contributor-message-v1.schema.json`](schema/contributor-message-v1.schema.json),
copied from Sonoran. See [docs/MESH-JOBS.md](docs/MESH-JOBS.md).

## ⚙️ Configuration

Every setting is an environment variable.

| Variable | Meaning | Default |
|----------|---------|:-------:|
| `STATION_ID` | Short name, `[a-z0-9-]{3,32}`. The place on the map. | — |
| `SOURCE_ID` | This Pi's name within the station, `[a-z0-9][a-z0-9-]{0,15}`. Required for a minted login; leave it out only for a login made by hand. | — |
| `BROKER_URL`, `BROKER_USER`, `BROKER_PASS` | The community broker, and this Pi's own login. | — |
| `SOURCES` | `kind=url` pairs, comma separated. Kinds: `adsb`, `uat`, `spectrum`, `aprs`, `sonde`, `ism`, `custom`. See [sources](#sources). | — |
| `STATION_LAT`, `STATION_LON` | Where the antenna is. | — |
| `STATION_PRECISION_M` | How much to round the station position before it is sent. | `1000` |
| `ADSB_INTERVAL_S` | Seconds between two points for one aircraft. At least 1. | `20` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. | `INFO` |
| `ISM_ALLOW` | `rtl_433` model names to pass, comma separated, case ignored. Empty means none pass. | empty |
| `ALERT_WEBHOOK` | Your own Discord webhook, for alerts about this station. See [alerts](#-alerts). | — |
| `SITE_MESH_NODE` | The Meshtastic node at this site, such as `!a1b2c3d4`, if any, so its own bursts are not read as interference. | — |
| `CUSTOM_INTERVAL_S` | Seconds between two points of one thing from a [mod](#-your-own-mods). | `10` |
| `CUSTOM_PER_MINUTE` | The most points one mod may send a minute. | `600` |

`rf-sweep` has settings of its own; see [spectrum](#-spectrum-the-900-mhz-band-and-any-other-range).

`BROKER_URL` must be `mqtts://`: the agent talks to a broker over TLS
only, checked against the system's certificate authorities. The port
defaults to 8883. `BROKER_USER` defaults to `STATION_ID`.

With `SOURCE_ID`, every message names the source, and the heartbeat goes
to `rf/v1/<station>/status/<source>`. A minted login may publish its
heartbeat there and nowhere else, so an agent with a minted login and no
`SOURCE_ID` is refused its heartbeat. Without `SOURCE_ID` the heartbeat
goes to `rf/v1/<station>/status`, as for a login made by hand.

`STATION_LAT` and `STATION_LON` are optional. Without them the heartbeat
carries no position, and surface positions of aircraft on the ground
cannot be decoded from Beast. The true position is used on the site to
decode and sanity-check positions; only the rounded one is sent. The
rounding snaps to a fixed grid, so heartbeats cannot be averaged back
to the antenna.

### Sources

The scheme of each URL picks the reader.

| URL | Reads | Default port |
|-----|-------|:------------:|
| `beast://host:port` | readsb or dump1090-fa Beast output | 30005 |
| `http://host/data/aircraft.json` | readsb or dump1090-fa `aircraft.json`, once a second | 80 |
| `file:///run/readsb/aircraft.json` | the same, from a mounted file | — |
| `tcp://host:port` | dump978-fa JSON lines | 30979 |
| `spectrum=tcp://host:port` | `rf-sweep` sweep lines | 30950 |
| `aprs=kiss://host:port` | direwolf's KISS stream | 8001 |
| `sonde=udp://0.0.0.0:port` | `radiosonde_auto_rx` summaries | 55672 |
| `ism=udp://0.0.0.0:port` | `rtl_433` JSON | — |
| `custom=udp://127.0.0.1:port` | your own [mod](#-your-own-mods): JSON lines | 30960 |

`radiosonde_auto_rx` and `rtl_433` only broadcast, so those two are
received on a UDP socket rather than connected to. The socket serves
nothing and answers nobody, and every datagram is treated as untrusted
input. Run `rtl_433` with `-F syslog:<agent>:<port>`, and turn every
internet upload off in `auto_rx`: this map carries what our own
receivers heard.

Only positions this receiver heard leave the site. From `aircraft.json`,
MLAT positions (computed by an aggregator's server and fed back) and
ADS-C (relayed from the internet) are left out. From Beast, TIS-B and
ADS-R rebroadcasts are left out; UAT aircraft come from `uat=`.
Kinds of a later phase are accepted in `SOURCES` and ignored, with a
warning, until their phase is built.

## 🔔 Alerts

Set `ALERT_WEBHOOK` to a Discord webhook (channel → Edit Channel →
Integrations → Webhooks → New Webhook → Copy Webhook URL), and the agent
posts there when something about your station changes:

- a decoder stops answering, or keeps closing the connection (readsb
  does that when its dongle is gone), or answers again;
- the broker is out of reach, or back;
- the sweep stalls (the same sweep over and over), or sends
  nothing for 3 minutes, which most often means the dongle is unplugged;
  and when fresh sweeps return.

One message when a problem starts or changes, and one when it ends.
Never one per retry. A decoder or the broker must be down for 2 minutes
before you hear about it, so a quick restart sends nothing, and you get
an "ended" message only for a problem you were told about. An error
that keeps switching between two forms is one problem, not news each
time. Alerts that come close together arrive as one message. The log
still records everything, as it happens. The URL holds a token: keep it out of anything you share. The
agent never logs it.

A Pi that loses power or its network cannot send its own alert.

## 📶 Spectrum: the 900 MHz band, and any other range

For a member with a **spare** dongle. `rf-sweep` owns that dongle and
does nothing else: it sweeps with `rtl_power`, at a fixed gain, and
serves the sweeps to the agent. The agent condenses each minute into
one message per range: for each bin, the median, the 95th percentile and
the peak level, and how often the bin stood more than 6 dB above its own
median. On 902 to 928 MHz, that last number is the one that says
"something keeps sitting on LongFast".

By default it sweeps 902 to 928 MHz, the band the mesh lives in. It can
sweep **any range you choose** instead, or several: a band beside the
mesh, a test you are running, whatever you want to watch. Sonoran marks
the Meshtastic channels only where a range covers them.

- **Several dongles:** run one `rf-sweep` per dongle, each with its own
  `SWEEP_SERIAL` and `SPECTRUM_RANGE`, and list each one in the agent:
  `SOURCES=spectrum=tcp://sweep-a:30950,spectrum=tcp://sweep-b:30950`.
  Each range is swept all the time.
- **One dongle, taking turns:** give `SPECTRUM_RANGES` a list. It sweeps
  each range for `SPECTRUM_DWELL_S` seconds, then the next, kept to the
  clock. **A dongle that takes turns between N ranges hears each only
  1/N of the time.** A burst on one range while it is on another is not
  heard. The map draws those minutes as "not swept", never as quiet.

```yaml
    environment:
      SWEEP_SERIAL: SWEEP915
      SWEEP_GAIN_DB: "32.8"
      SPECTRUM_RANGES: 902M:928M:25k:name=ism,420M:450M:25k:name=uhf
      SPECTRUM_DWELL_S: "60"
```

Frequencies take a `k`, `M` or `G`. `name=` is optional: a short label,
`a-z`, `0-9` and `-`, for the range picker. An RTL-SDR tunes about
24 MHz to 1.766 GHz; a range outside that is refused at start, because
`rtl_power` would sweep nothing and the map would show a quiet band.
At most 8 ranges, and at most 4,096 bins in each.

The agent reports what `rf-sweep` runs in its heartbeat: the tool and its
version, each range, the bin, the gain, the interval, and the exact
`rtl_power` command line, with the dongle's serial left out. The map shows
it beside your station, as reported by the station. Nothing is ever run
from the broker or from Sonoran: `rf-sweep` builds its command from its
own settings, and only says what it built.

A dongle that is unplugged or stalls does not make `rtl_power` exit: it
prints its last sweep over and over, and never opens the dongle again.
The agent drops a sweep exactly the same as the one before it and sends
no minute that holds only repeats, so the map shows a gap, not a flat,
quiet band. After about a minute of repeats, or two minutes with no
sweep at all, `rf-sweep` restarts `rtl_power` itself. It keeps trying,
more slowly each time, until the dongle is back, so a dongle plugged
back in is found again with no one restarting anything.

That needs the USB bus as a **volume** with a device rule, as below, not
as `devices:`. `devices:` copies the USB device nodes once, when the
container starts. A dongle plugged in later gets a new node, which the
container never sees: only a container restart would find it. `189` is
the Linux device number of USB devices; the rule lets the container open
them, including ones plugged in after it started.

```yaml
services:
  rf-sweep:
    image: ghcr.io/arizonameshtasticcommunity/rf-sweep:main
    restart: unless-stopped
    logging:
      options:
        max-size: "10m"
        max-file: "3"
    volumes:
      - /dev/bus/usb:/dev/bus/usb
    device_cgroup_rules:
      - "c 189:* rmw"
    environment:
      SWEEP_SERIAL: SWEEP915
      SWEEP_GAIN_DB: "32.8"
  rf-agent:
    image: ghcr.io/arizonameshtasticcommunity/rf-agent:main
    restart: unless-stopped
    logging:
      options:
        max-size: "10m"
        max-file: "3"
    environment:
      STATION_ID: tempe-roof
      BROKER_URL: mqtts://broker.example:8883
      BROKER_PASS: ${RF_AGENT_PASS}
      SOURCES: spectrum=tcp://rf-sweep:30950
      SITE_MESH_NODE: "!a1b2c3d4"
```

`rf-sweep` is optional. The agent runs without it, for aircraft only, and
a site with only a spare dongle can list `spectrum=` alone. Each source
stands on its own: if `rf-sweep` is down, the agent keeps retrying it
while aircraft keep flowing, and spectrum starts again when it is back.

| `rf-sweep` variable | Meaning | Default |
|---------------------|---------|:-------:|
| `SWEEP_SERIAL` | The serial of the dongle to sweep with. It opens that one and no other. | — |
| `SWEEP_GAIN_DB` | The gain, fixed, in dB. Automatic gain is refused. | — |
| `SPECTRUM_RANGE` | `start:end:bin`, one range. | `902000000:928000000:50000` |
| `SPECTRUM_RANGES` | Several, comma separated, each `start:end:bin[:name=x]`, swept in turn. Not with `SPECTRUM_RANGE`. | — |
| `SPECTRUM_DWELL_S` | Seconds on each range in turn, 10 to 3600. | `60` |
| `SWEEP_INTERVAL_S` | Seconds per sweep. | `5` |
| `SWEEP_PPM` | The dongle's frequency correction. | `0` |
| `SWEEP_PORT` | The port the agent reads, inside the compose network. | `30950` |

Before the first run:

- **Give the dongle a serial of its own**, with letters in it, so
  `rf-sweep` can never take a dongle that feeds something else. With
  only that dongle plugged in: `rtl_eeprom -s SWEEP915`, then unplug it
  and plug it in again. An all-digit serial is refused, because
  `rtl_power` would read it as a device number.
- **Pick the gain once and keep it.** A level only compares with the
  same station at the same gain. Change the gain and the minutes before
  and after no longer compare.
- **Keep it away from the site's own node.** A sweeper beside a
  Meshtastic node mostly records that node. `SITE_MESH_NODE` lets
  Sonoran mark the minutes the node was talking.
- **Do not publish port 30950** on the host. Only the agent needs it.

Levels are in dB relative to the dongle's full scale, not dBm. They
compare one station with itself over time, never two stations. The
band view shows what occupies the band; it does not say what the thing
is. We store power levels only, never what was said: an operator may
still choose not to store some ranges at all.

## 🧩 Your own mods

Want to send something none of the built-in decoders read? Write a small
program (a "mod") that reads **your own receiver** and sends what it
hears to the agent, as JSON lines over UDP. The agent checks each line
and sends it as your station's data, on your login, with your heartbeat.
Sonoran shows it in its own "Member mods" layer, marked as from your mod
and unverified.

```yaml
      SOURCES: adsb=beast://readsb:30005,custom=udp://127.0.0.1:30960
```

```sh
echo '{"mod": "lightning", "id": "strike-1", "heard_here": true, "meta": {"distance_km": 12}}' \
  | nc -u -w1 127.0.0.1 30960
```

The rules are the same as for everything else: only what your own
receiver heard, never data from the internet; positions are yours to
round. See [docs/MODS.md](docs/MODS.md) for the format, the limits, and a
complete example.

## 📻 APRS, radiosondes and sensors

Three more decoders, each already serving what the agent needs.

```yaml
      SOURCES: >-
        aprs=kiss://direwolf:8001,
        sonde=udp://0.0.0.0:55672,
        ism=udp://0.0.0.0:10433
      ISM_ALLOW: Acurite-Tower,Fineoffset-WH51
```

- **APRS** is read from direwolf's KISS stream: uncompressed, compressed
  and Mic-E positions all decode, including positions a station has
  blurred on purpose, which say how blurred they are. Objects and items a
  station places are sent as things of their own, named `;` and the
  object's name, with the sender in `meta.owner`. Packets with no
  position are skipped, and so are killed objects.
  What a radio adds around a comment is taken off it: its own device
  code, its telemetry, and the DAO extension, whose extra digit of
  precision is applied to the position rather than thrown away. A
  weather station that measures no wind sends dots, and those are left
  out rather than stored as a calm day.
  Each point says whether we heard the station **directly**, and names
  the digipeater or gateway we heard instead when we did not: a packet
  relayed from 150 km away says where that station is, but nothing about
  what your receiver can hear. One packet arriving by several paths is
  sent once. APRS is public by design and is stored as heard.
- **Radiosondes** come from `radiosonde_auto_rx`, whose summaries carry
  the sonde's own GPS position. Thinning cuts a two hour flight to one
  point every five seconds.
- **Weather sensors** come from `rtl_433`, **and nothing passes until
  you name the models**. `rtl_433` decodes tyre sensors, doorbells and
  car keys in neighbours' houses as readily as weather stations, so
  `ISM_ALLOW` is empty by default and Sonoran applies the same list
  again. Run `rtl_433 -F syslog:<agent host>:10433`; the model name is
  the one it prints, such as `Acurite-Tower`.

A sensor transmits no position, so its point carries the station's
rounded position. It says where the sensor was heard, not where it is.

## 📨 The message contract

| Topic | Message | QoS |
|-------|---------|:---:|
| `rf/v1/<station>/<kind>` | a **batch** of up to 100 observations of one kind | 0 |
| `rf/v1/<station>/spectrum` | one **minute** of the 900 MHz band | 0 |
| `rf/v1/<station>/status` | the **heartbeat**, every 60 s, retained | 1 |

A message names the station its topic names, or Sonoran drops it. Unknown
fields are ignored, so an agent may send more than the contract; a field
that is present but wrong makes the whole message invalid.

| Kind | Leaves the site |
|------|-----------------|
| `adsb`, `uat` | one point per aircraft every `ADSB_INTERVAL_S`, plus the first and last of each visit |
| `sonde` | one point every 5 s |
| `aprs`, `ism` | every packet, as received: both are slow already |
| `spectrum` | one summary per minute |

Examples of each message are in [`examples/`](examples). The heartbeat's
`counts` say how much was heard, sent and thinned since the last one: a
thinned picture must say it is thinned.

## 🛠️ Development

Python 3.13 and `aiomqtt`, like Sonoran.

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

```bash
uv run ruff check src tests tools && uv run ruff format --check src tests tools
```

The readers are tested against captures in each decoder's own format,
in [`tests/fixtures/`](tests/fixtures). Every message a test builds is
checked against the contract with `tools/validate.py`. The captures are
written by `tests/fixtures/make_captures.py`, for invented aircraft round
a point where no station stands. A real recording may replace one, once
every position in it is moved with
[`tools/shift_capture.py`](tools/shift_capture.py), which drops any frame
whose position it cannot move.

Check any message against the contract:

```bash
uv run --with jsonschema --with rfc3339-validator python tools/validate.py examples/*.json
```

A message that passes here is one Sonoran accepts. The schema is generated
from Sonoran's `sonoran/rf/schema.py`; copy it here when the contract
changes, never edit it by hand.

## 🗺️ Roadmap

1. **Aircraft** — ADS-B and UAT readers, thinning, heartbeat. Built, here and in Sonoran.
2. **Spectrum** — `rf-sweep` and the spectrum summary: the 900 MHz band, any range, several in turn, and the sweep each station reports. Built, here and in Sonoran.
3. **APRS** — the APRS reader, off the air. Built.
4. **Radiosondes and weather sensors** — two more readers and the allow-list. Built.
5. **Members' mods** — the `custom` input and the library in `rf_agent.messages`. Built, here and in Sonoran.

## 📚 Documentation

| Document | What it covers |
|----------|----------------|
| [`docs/DESIGN.md`](docs/DESIGN.md) | The full design: scope, transport, storage, capacity, privacy. A mirror of Sonoran's `docs/design/rf-map.md`; sections 3 and 4 are owned here |
| [`AGENTS.md`](AGENTS.md) | The rules for anyone, person or agent, working in this repository |
| [`schema/rf-message-v1.schema.json`](schema/rf-message-v1.schema.json) | The message contract |
| [`docs/MODS.md`](docs/MODS.md) | Writing your own mod: the format, the limits, and a complete example |
| [`examples/`](examples) | One valid message of each kind, and an example mod |

## 📄 Licence

Not chosen yet.
