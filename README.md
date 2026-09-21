<div align="center">

# rf-map

**What the community's own radios hear, beyond Meshtastic.**
Local receivers · MQTT to one broker · stored and mapped by Sonoran.

[![Contract](https://img.shields.io/badge/contract-v1-EA6A3A?style=flat-square)](schema/rf-message-v1.schema.json)
[![Phase](https://img.shields.io/badge/phase-1%20aircraft-2A6DB2?style=flat-square)](#-roadmap)
[![Sources](https://img.shields.io/badge/sources-local%20only-3776AB?style=flat-square)](#-what-it-reads)
[![Sonoran](https://img.shields.io/badge/stored%20by-Sonoran-EA6A3A?style=flat-square)](https://github.com/ArizonaMeshtasticCommunity/beta-sonoran)

</div>

---

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

- ✈️ **Aircraft** — ADS-B at 1090 MHz and UAT at 978 MHz, from `readsb`, `dump1090-fa` or `dump978-fa`.
- 🤝 **Never takes your dongle** — reads the decoder's network port, so FlightAware, FR24 and every other feed keep running.
- 🪶 **Thinned at the edge** — one point per aircraft every 20 s leaves the site, not one a second.
- 📡 **Receiver reach** — each station's heartbeat says where it is and what it heard, so the map shows where the network hears, measured.
- 🔒 **Private by default** — station positions rounded before they leave; weather-sensor decoding is allow-list only.
- 📜 **One contract** — [`schema/rf-message-v1.schema.json`](schema/rf-message-v1.schema.json), the same file Sonoran checks every message against.

## 🧩 How it fits together

```
member's site (one or more SDRs)                       Sonoran host
┌──────────────────────────────────────┐
│ readsb ──:30005──┐                   │
│ dump978 ─:30978──┤                   │   MQTT over TLS
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
| UAT | `dump978-fa` | JSON on TCP 30978 | 978 MHz | **1** |
| Spectrum | `rtl_power` in `rf-sweep`, its own dongle | CSV sweep lines | 902–928 MHz | 2 |
| APRS | `direwolf`, off the air | KISS on TCP 8001 | 144.39 MHz | 3 |
| Radiosondes | `radiosonde_auto_rx`, upload off | UDP 55672 | 400–406 MHz | 4 |
| Weather sensors | `rtl_433`, allow-listed | JSON over UDP | 433 / 915 MHz | 4 |

## 🚀 Quick start

Add one service beside whatever you already run. No device passthrough, no
privileges, no listening ports.

```yaml
services:
  rf-agent:
    image: ghcr.io/arizonameshtasticcommunity/rf-agent:1
    restart: unless-stopped
    environment:
      STATION_ID: tempe-roof
      BROKER_URL: mqtts://broker.example:8883
      BROKER_USER: tempe-roof
      BROKER_PASS: ${RF_AGENT_PASS}
      SOURCES: adsb=beast://readsb:30005,uat=tcp://dump978:30978
      STATION_LAT: "33.42"
      STATION_LON: "-111.94"
      STATION_PRECISION_M: "1000"
```

Ask an operator for a station login first. Each login may publish to its
own topic and nothing else.

> [!NOTE]
> The agent itself is being built. The contract, the examples and the
> validator in this repository are ready to build against now.

## ⚙️ Configuration

Every setting is an environment variable.

| Variable | Meaning | Default |
|----------|---------|:-------:|
| `STATION_ID` | Short name, `[a-z0-9-]{3,32}`. Also the broker login. | — |
| `BROKER_URL`, `BROKER_USER`, `BROKER_PASS` | The community broker, and this station's own login. | — |
| `SOURCES` | `kind=url` pairs, comma separated. Kinds: `adsb`, `uat`, `aprs`, `sonde`, `ism`. | — |
| `STATION_LAT`, `STATION_LON` | Where the antenna is. | — |
| `STATION_PRECISION_M` | How much to round the station position before it is sent. | `1000` |
| `ADSB_INTERVAL_S` | Seconds between two points for one aircraft. | `20` |
| `ISM_ALLOW` | `rtl_433` model names to pass. Empty means none pass. | empty |
| `SPECTRUM_RANGE` | `start:end:bin` in Hz, for `rf-sweep`. | `902000000:928000000:50000` |
| `SITE_MESH_NODE` | The Meshtastic node at this site, if any, so its own bursts are not read as interference. | — |

## 📨 The message contract

| Topic | Message | QoS |
|-------|---------|:---:|
| `rf/v1/<station>/<kind>` | a **batch** of up to 100 observations of one kind | 0 |
| `rf/v1/<station>/status` | the **heartbeat**, every 60 s, retained | 1 |

A message names the station its topic names, or Sonoran drops it. Unknown
fields are ignored, so an agent may send more than the contract; a field
that is present but wrong makes the whole message invalid.

| Kind | Leaves the site |
|------|-----------------|
| `adsb`, `uat` | one point per aircraft every `ADSB_INTERVAL_S`, plus the first and last of each visit |
| `sonde` | one point every 5 s |
| `aprs`, `ism` | every packet, as received |
| `spectrum` | one summary per minute |

Examples of each message are in [`examples/`](examples). The heartbeat's
`counts` say how much was heard, sent and thinned since the last one: a
thinned picture must say it is thinned.

## 🛠️ Development

Check any message against the contract:

```bash
uv run --with jsonschema --with rfc3339-validator python tools/validate.py examples/*.json
```

A message that passes here is one Sonoran accepts. The schema is generated
from Sonoran's `sonoran/rf/schema.py`; copy it here when the contract
changes, never edit it by hand.

## 🗺️ Roadmap

1. **Aircraft** — ADS-B and UAT readers, thinning, heartbeat. Sonoran's side is built.
2. **The 900 MHz band** — `rf-sweep` and the spectrum summary.
3. **APRS** — the APRS reader, off the air.
4. **Radiosondes and weather sensors** — two more readers and the allow-lists.

## 📚 Documentation

| Document | What it covers |
|----------|----------------|
| [`docs/DESIGN.md`](docs/DESIGN.md) | The full design: scope, transport, storage, capacity, privacy. A mirror of Sonoran's `docs/design/rf-map.md`; sections 3 and 4 are owned here |
| [`AGENTS.md`](AGENTS.md) | The rules for anyone, person or agent, working in this repository |
| [`schema/rf-message-v1.schema.json`](schema/rf-message-v1.schema.json) | The message contract |
| [`examples/`](examples) | One valid message of each kind |

## 📄 Licence

Not chosen yet.
