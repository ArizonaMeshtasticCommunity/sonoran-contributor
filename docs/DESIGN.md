<!--
  Mirror of Sonoran's docs/design/rf-map.md, copied at d45c318 on the
  design/rf-map branch (PR #68). Sections 3 and 4 (rf-agent, transport and
  the message contract) are owned here. Sections 5 to 7 (storage, API, the
  RF tab) are owned by Sonoran. Change a shared part in both places.
-->

# RF map: what our own receivers hear, beyond Meshtastic

**State:** Phase 1 (aircraft) is built on the sonoran side:
`sonoran-rf-ingest`, revision 0013, `/api/rf`, and the RF page. The
message contract is `sonoran/rf/schema.py`, published as
`docs/rf-message-v1.schema.json`. `rf-agent` is built separately.
Phases 2 to 4 are design only.

This document answers one request: a new tab that maps what the
community's own radios receive. Aircraft first, then APRS, radiosondes
and weather sensors. It also watches the 902 to 928 MHz band that
Meshtastic uses, to find the interference that hurts the mesh. Every
point on it must come from a receiver one of us runs. Nothing is
fetched from the internet.

It adds two things to the system:

1. **`rf-agent`**, a small separate app that runs beside a member's
   SDR, reads the decoder they already run, and publishes to our MQTT
   broker.
2. **`rf-ingest`**, a new sonoran process that stores those messages,
   with read-only API endpoints and an **RF** tab on the dashboard.

The shape is the one sonoran already has for Meshtastic: receivers,
then the broker, then an ingest process, then TimescaleDB, then a GET
only API, then the web page. Most of the plumbing is proven.

---

## 1. Scope

### 1.1 In

| Source | Decoder on the receiver's machine | Band |
|---|---|---|
| ADS-B | `readsb` or `dump1090-fa` | 1090 MHz |
| UAT (US general aviation) | `dump978-fa` | 978 MHz |
| APRS, off the air | `direwolf` | 144.39 MHz |
| Radiosondes | `radiosonde_auto_rx`, internet upload off | 400 to 406 MHz |
| Weather sensors | `rtl_433`, allow-listed device types only | 433 and 915 MHz |
| **Spectrum**, the band Meshtastic uses | `rtl_power` (or `hackrf_sweep`), on its own dongle | 902 to 928 MHz |

### 1.2 Out, on purpose

- **Every internet source.** No APRS-IS, no ADS-B aggregator API, no
  SondeHub, no satellite TLE download. A point we did not receive
  ourselves is a claim we cannot check, and some of those sources
  forbid republishing.
- **Anything that needs the SDR itself.** `rf-agent` never opens a USB
  device. See 3.1.
- **AIS.** There are no ships to hear in Arizona.
- **Pager and other personal traffic.** Not decoded, not stored.

### 1.3 What it must never claim

The map shows what our receivers heard. It is not air traffic control,
not a complete picture of the sky, and not a safety tool. An aircraft
that is not on it was not heard by us. The tab says so where it cannot
be missed, the same way the Meshtastic map says an observer list is a
list of gateways (`AGENTS.md` 9.5.1).

---

## 2. Architecture

```
member's site (one or more SDRs)                       sonoran host
┌──────────────────────────────────────┐
│ readsb ──:30005──┐                   │
│ dump978 ─:30979──┤                   │   MQTT over TLS
│ direwolf ─:8001──┼──► rf-agent ──────┼──────────────► broker
│ rtl_433 ──UDP────┤   (read-only)     │                  │
│ auto_rx ──UDP────┘                   │                  ▼
└──────────────────────────────────────┘            rf-ingest
                                                         │
                                                    TimescaleDB
                                                         │
                                          API (GET) ──► RF tab
```

Transport is the community's own MQTT broker, over TLS. The data is
local in the sense that matters: our receivers, our broker, our
database. It travels over the internet only between a member's site and
the sonoran host.

---

## 3. `rf-agent`

A separate repository and image, because it runs on members' machines,
usually a Raspberry Pi. Proposed: `ArizonaMeshtasticCommunity/rf-agent`.

### 3.1 It never touches the SDR

A USB SDR can be opened by one program at a time. Many members already
feed FlightAware, FR24 or others from one dongle through `readsb`. If
`rf-agent` opened the device, it would take it from them.

So `rf-agent` reads the network output the decoder already offers.
Every decoder in 1.1 serves several readers at once. That is how one
dongle feeds five aggregators today.

Two of them only broadcast: `rtl_433` and `radiosonde_auto_rx` send
datagrams and accept no connection. Those the agent receives on a UDP
socket, which serves nothing and answers nobody, and every datagram is
treated as untrusted input. Everything else it connects out to.

A weather sensor transmits no position of its own. Its point carries the
station's rounded position, so it is drawn where it was heard, not where
it is: every sensor one station hears lands on the same point, and two
stations hearing one sensor will disagree about where it is.

| Decoder | What `rf-agent` reads |
|---|---|
| `readsb`, `dump1090-fa` | Beast on TCP 30005, or `aircraft.json` over HTTP |
| `dump978-fa` | JSON on TCP 30979 |
| `direwolf` | KISS on TCP 8001 (several clients allowed) |
| `rtl_433` | JSON over UDP |
| `radiosonde_auto_rx` | its UDP summary broadcast, port 55672 |
| `rtl_power` | its CSV sweep lines, on a pipe or a file (see 3.5) |

### 3.2 Two ways to run it

**Add-on (the default).** The member keeps everything they run. They add
one container, pointed at their decoder. No device passthrough, no
privileges, no listening ports. Their other feeds are not affected.

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
      SOURCES: adsb=beast://readsb:30005,uat=tcp://dump978:30979
      STATION_LAT: "33.42"
      STATION_LON: "-111.94"
      STATION_PRECISION_M: "1000"
```

**Standalone.** For a member with a spare dongle, a compose bundle that
also runs the decoder, from the existing `sdr-enthusiasts` images. The
dongle is chosen by its serial number, set once with `rtl_eeprom`, so
the bundle cannot take a dongle that belongs to something else. The
decoder re-shares its output, so the member can add other feeds later.

### 3.3 What it does

1. **Read** each configured source with a small reader per format.
2. **Normalise** to the message in 4.2.
3. **Thin** at the edge, before anything leaves the site. See 4.3.
4. **Publish** to its own topic, and a heartbeat every 60 s.
5. **Reconnect** to a decoder or the broker with backoff, and never
   exit on a bad frame. A decoder that restarts must not need the agent
   restarted.

It is expected to use a few percent of one core and about 50 MB.

### 3.4 Configuration

Environment variables only, so a compose block is the whole setup.

| Variable | Meaning |
|---|---|
| `STATION_ID` | Short name, `[a-z0-9-]{3,32}`. Also the broker login. |
| `BROKER_URL`, `BROKER_USER`, `BROKER_PASS` | The broker, and this station's own login. |
| `SOURCES` | `kind=url` pairs. Kinds: `adsb`, `uat`, `aprs`, `sonde`, `ism`. |
| `STATION_LAT`, `STATION_LON` | Where the antenna is. |
| `STATION_PRECISION_M` | How much to round the station position before it is sent. Default 1000. |
| `ADSB_INTERVAL_S` | Seconds between two points for one aircraft. Default 20. |
| `ISM_ALLOW` | `rtl_433` model names to pass. Empty means none pass. |
| `SPECTRUM_RANGE` | Default `902000000:928000000:50000`, start, end and bin in Hz. |
| `SITE_MESH_NODE` | The Meshtastic node ID at this site, if any. See 3.5. |

### 3.5 The spectrum sweeper

A spectrum sweep is the one source with no decoder to share: the
sweeper owns its dongle for as long as it runs. So it runs as its own
small container, `rf-sweep`, beside the agent, on a dongle chosen by
serial number. The agent reads its output and never opens the device,
which keeps the rule in 3.1.

- It sweeps 902 to 928 MHz in 50 kHz bins, about 520 bins, once every
  few seconds. 50 kHz resolves a 125 or 250 kHz LoRa channel.
- **The gain is fixed**, never automatic. With automatic gain the level
  moves with whatever else is on the air, and a week of readings could
  not be compared with itself.
- An RTL-SDR reads in dB relative to its full scale, not in dBm. Its
  numbers compare one station with itself over time. They never compare
  two stations, and the tab must not draw them as if they did.
- **Placement matters.** A sweeper beside the site's own Meshtastic
  node mostly records that node. The station heartbeat names the node
  on the same site, if there is one, so the ingest can mark the minutes
  that node was transmitting.
- The member needs a 915 MHz antenna and a dongle that does nothing
  else. It cannot share with ADS-B.

The agent condenses each minute of sweeps into one message per bin:
the median, the 95th percentile, the peak, and the share of sweeps in
which the bin stood more than 6 dB above its own median. See 4.2.

---

## 4. Transport

### 4.1 Topics and logins

```
rf/v1/<station_id>/<kind>        # observations
rf/v1/<station_id>/status        # heartbeat, retained
```

Each station gets its own broker login. Its ACL lets it publish to
`rf/v1/<its own id>/#` and nothing else, and subscribe to nothing.
`rf-ingest` gets one read-only login for `rf/v1/#`. Neither login can
touch `msh/#`. The logins are made by hand for now; a member asks, an
operator creates one.

QoS 0 for observations: a lost point is replaced by the next one 20 s
later. The heartbeat is QoS 1 and retained, so the ingest knows every
station's last state as soon as it connects.

### 4.2 The message

JSON, one object per message, a batch of up to 100 observations.

```json
{
  "v": 1,
  "station": "tempe-roof",
  "kind": "adsb",
  "sent": "2026-09-22T18:00:00Z",
  "obs": [
    {
      "id": "a1b2c3",
      "t": "2026-09-22T17:59:58.4Z",
      "lat": 33.51, "lon": -112.02,
      "alt_m": 3350, "alt_ref": "baro",
      "gs_kn": 212, "track_deg": 84,
      "rssi_db": -24.1,
      "meta": { "callsign": "SWA1234", "squawk": "1200", "category": "A3" }
    }
  ]
}
```

- `id` is the natural identity of the thing heard: the ICAO hex for
  aircraft, the callsign and SSID for APRS, the serial for a sonde, the
  model and id for a sensor.
- `t` is when the receiver heard it, from the receiver's clock. See 5.3.
- `rssi_db` is kept because it is the only thing that says how well a
  station hears. It is what the receiver layer in 7.2 is built from.
- `meta` is per kind and capped at 1 kB.

A `spectrum` message is different: one per minute, with the bins as
arrays rather than a list of things heard.

```json
{
  "v": 1, "station": "tempe-roof", "kind": "spectrum",
  "t": "2026-09-22T18:00:00Z",
  "f_start_hz": 902000000, "bin_hz": 50000, "sweeps": 12,
  "gain_db": 32.8,
  "p50": [-512, -509, ...], "p95": [-470, ...], "peak": [-402, ...],
  "busy": [3, 0, 41, ...]
}
```

Levels are tenths of a dB relative to full scale, as integers, so a
minute of 520 bins is a few kilobytes. `busy` is the share of sweeps, in
percent, in which the bin stood more than 6 dB above its median. `t` is
the start of the minute. The four arrays have one entry per bin, and a
bin no sweep reached in that minute is `null`, never a made-up level.

The heartbeat carries the rounded station position, the kinds it
serves, the agent version, and counts: heard, sent, and dropped by
thinning, since the last heartbeat. A thinned picture must say it is
thinned. It also names the Meshtastic node on the same site, if any, as
`mesh_node` (`!a1b2c3d4`), for 3.5.

### 4.3 Thinning at the edge

Without it, ADS-B is about one message per aircraft per second.

| Kind | Rule |
|---|---|
| `adsb`, `uat` | One point per aircraft every `ADSB_INTERVAL_S` (20 s), plus the first and last point of each visit. |
| `sonde` | One point every 5 s. A flight lasts about two hours. |
| `aprs`, `ism` | Every packet, as received. They are already slow. |
| `spectrum` | One summary per minute, whatever the sweep rate. |

---

## 5. `rf-ingest` and storage

### 5.1 The process

A new process beside `ingest`, in the same image, started as
`sonoran-rf-ingest`, behind the compose profile `rf`, so it runs only
where it is asked for. It follows the same rules as the Meshtastic
ingest: INSERT and UPDATE only, SELECT on nothing but the columns a
conflict target or an `UPDATE ... WHERE` reads (`AGENTS.md` 4, 9.1, 9.2),
and two statements instead of `ON CONFLICT DO UPDATE`.

Phase 1 runs it as `azmsh_ingest` rather than a role of its own. A new
role is a new password on every deployment, and the ingest role already
holds exactly the shape of rights it needs. When RF runs on a host of
its own, it gets its own role.

It validates every message against the schema in 4.2, drops what does
not fit, and counts what it dropped per station. A bad station must not
be able to stop the ingest.

### 5.2 Tables

| Table | Kind | Holds | Retention |
|---|---|---|---|
| `rf_station` | plain | one row per station: rounded position, kinds, last heartbeat, last counts | kept |
| `rf_track` | hypertable on `t` | one point per thing per time bucket, from whichever station heard it best | ADS-B/UAT 48 h, sondes 30 d, APRS 30 d, sensors 7 d |
| `rf_heard` | hypertable on `bucket` | per station, per thing, per 5 min: best RSSI and count | 14 d |
| `rf_latest` | plain | the newest point of each thing, updated in place | rows older than 1 h deleted |
| `rf_spectrum` | hypertable on `t` | one row per station per minute: the four arrays of 3.5 | 14 d, then an hourly summary kept 180 d |

`rf_track` deduplicates across stations. Two receivers hearing the same
aircraft make one track point, not two. The identity is
(`kind`, `thing_id`, 20 s bucket), the same idea as (`from_node_num`,
`packet_id`) for Meshtastic (`AGENTS.md` 9.1). Which stations heard it
lives in `rf_heard`, so the dedup loses nothing.

### 5.3 Time

The receiver stamps `t`, and receivers' clocks can be wrong. That trap
cost sonoran a day of debugging for Meshtastic (`AGENTS.md` 9.4). Here
the stations are ours and run NTP, so `t` is trusted, with a guard: a
point more than 60 s ahead of our receive time, or more than 10 min
behind it, is stored with our receive time instead, and counted.

### 5.4 Capacity

The deployment host is small and shared with the database, the broker
and the rest of sonoran. The design must fit on it, so the arithmetic
is written here.

- A good Phoenix receiver hears about 50 to 200 aircraft at once. Take
  300 across the state after dedup.
- One point per aircraft per 20 s: 300 / 20 = **15 points a second**,
  about **1.3 million rows a day**.
- 48 h retention holds about **2.6 million rows**. At roughly 100 bytes
  a row before compression, that is about 260 MB, less once TimescaleDB
  compresses chunks older than 6 h.
- For scale, sonoran's `packet_seen` takes about 260,000 rows a day.
  ADS-B would be about five times that, so the 20 s interval and the
  48 h retention are load bearing. Neither may be lowered without
  measuring first.
- `rf-ingest` itself should stay under 100 MB of memory.

Spectrum is small by comparison: one row per station per minute, 1,440
a day. Four arrays of 520 two byte values are about 4 kB a row, so about
6 MB per station per day before compression, and well under that after.
The hourly summary is what keeps six months affordable.

If the numbers are worse than this when measured, the fallback is a
longer interval, not a bigger box.

---

## 6. API

GET only, like everything else (`AGENTS.md` 4).

| Endpoint | Answers |
|---|---|
| `GET /api/rf/stations` | every station, its rounded position, kinds, and when it last reported |
| `GET /api/rf/latest?kind=&since=` | the newest point of everything heard in the window |
| `GET /api/rf/track/{kind}/{id}?hours=` | one thing's track |
| `GET /api/rf/heard/{station}?hours=` | what one station heard, for its range plot |
| `GET /api/rf/spectrum/{station}?hours=` | one station's minutes, for the waterfall |
| `GET /api/rf/channels` | where each Meshtastic preset sits in the band, see 7.4 |

Bounded like the existing endpoints: a `limit` with a hard cap, and the
count of the whole window in a header, never a silent cut
(`AGENTS.md` 9.19).

---

## 7. The RF tab

A new tab, **RF**, beside the Live map. Leaflet, with the canvas engine
from `map-live.js` reused for moving marks.

### 7.1 Layers

A toggle per kind, remembered per browser, the same pattern as the
packet kind filter on the Live map (issue #48):

- **Aircraft**: an arrow in the direction of travel, with a short trail.
  Colour by altitude, never by anything that looks like a risk.
- **APRS**: the station, and its path when it moves.
- **Radiosondes**: the flight path, with the predicted landing area
  left out, because it would be a prediction.
- **Sensors**: a dot per allowed weather station, with its last reading.

### 7.2 Receivers

A layer of our own stations, each with the area it has actually heard
aircraft in, built from `rf_heard`. The coverage of the network, in
measured points, without a model. Where the Meshtastic coverage map is
a prediction, this is a measurement, and it must be drawn so the two
can never be confused.

### 7.3 Watch mode

The Live map's full-screen watch mode, reused. Most of the use of this
tab will be leaving it on a screen.

### 7.4 The 900 MHz band

The part of the tab that watches for interference to the mesh.

- **A waterfall per station**: frequency across, time down, the 95th
  percentile level as colour. One station at a time, for the reason in
  3.5.
- **Our channels marked on it.** Each preset's centre frequency is
  computed the way the firmware does it: the slot is the djb2 hash of
  the channel name modulo the number of slots, unless the channel sets
  `channel_num`, and the centre is `freqStart + bw/2 + slot × bw`. For
  the US region, `freqStart` is 902 MHz. The bands are drawn from that,
  never typed in by hand.
- **Busy over time, per preset channel**: the `busy` share inside each
  of our channel bands, as a line. This is the number that answers
  "is something sitting on LongFast".
- **Our own traffic is marked**, from the site's Meshtastic node (3.5),
  so a busy minute that was our own node talking is not read as
  interference.

It shows what occupies the band. It does not say what the thing is.
Smart meters hop across the whole band, LoRaWAN gateways and Z-Wave sit
on known frequencies, and amateur 33 cm traffic shares the band with
more rights than we have. Naming a source is left to the person reading
the waterfall; the tab offers the pattern, not a verdict.

---

## 8. Privacy

- **Aircraft on the FAA LADD and PIA programmes** ask not to be tracked
  publicly. The tab must be able to hide a list of ICAO addresses. The
  list is loaded by an operator, not fetched. Until it exists, the RF
  tab is not public.
- **`rtl_433`** decodes tyre sensors, doorbells and car keys in
  neighbours' houses as easily as weather stations. `ISM_ALLOW` is
  empty by default, so nothing passes until a member names the models.
  The ingest applies the same allow-list again.
- **Station positions** are rounded at the agent, to 1 km by default.
  The member chooses.
- **APRS** is public by design. It is stored as heard.

---

## 9. Build order

Each phase is useful on its own and ends with a measurement.

1. **Phase 1: aircraft.**
   - `rf-agent` with the Beast and `aircraft.json` readers, thinning,
     heartbeat.
   - One station login on the broker, by hand.
   - `rf-ingest`, the four tables, the migration, two endpoints.
   - The RF tab with the aircraft and receiver layers.
   - One test site with one SDR. Measure rows per day, ingest memory,
     and database growth against 5.4 before a second site joins.
2. **Phase 2: the 900 MHz band.** `rf-sweep`, the spectrum reader,
   `rf_spectrum`, and the band view of 7.4. One site with a spare
   dongle and a 915 MHz antenna, away from its own node.
3. **Phase 3: UAT and APRS.** Two more readers, the APRS layer.
4. **Phase 4: radiosondes and sensors.** Two more readers, the
   allow-lists, the sonde and sensor layers.

Each phase is one PR in sonoran, and one release of `rf-agent`.

---

## 10. Testing

- **Agent readers** are tested with recorded decoder output: a Beast
  capture, an `aircraft.json` file, a KISS frame stream. Recorded from
  a real receiver, with the positions shifted, so no real station is in
  the repository.
- **Thinning** has unit tests that count points in and out.
- **The channel rule** in 7.4 has a test per preset against the
  frequencies the firmware reports for the same channel names.
- **Spectrum summaries** are tested with a recorded `rtl_power` capture:
  a known carrier must land in the right bin and raise its `busy`.
- **`rf-ingest`** has integration tests against TimescaleDB, like the
  rest: dedup across two stations, the time guard in 5.3, a malformed
  message dropped and counted.
- **Load:** a replay of one hour of a busy receiver at ten times speed,
  run against the dev database, before Phase 1 goes live. The load test
  and any other heavy job never run at the same time on the host.

---

## 11. Open decisions

| Decision | Proposed |
|---|---|
| Where `rf-agent` lives | Its own repository and image, open source, so members can read what they run. |
| Which broker | The community broker, over TLS, with a login per station. |
| Who gets a station login | Asked for by the member, created by an operator. |
| Public or not | Not public until the LADD/PIA list exists. |
| Where it runs if 5.4 does not hold | A longer interval first. A separate host only if that fails. |
| Noise measured by our own nodes | Also store Meshtastic `LocalStats` (`noise_floor`, `num_packets_rx_bad`), which sonoran's ingest drops today. A separate, small sonoran change: it says how noisy it is at each node, where the sweeper says what the noise is. |
