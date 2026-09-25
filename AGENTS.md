# AGENTS.md

Rules for anyone, person or AI agent, working in this repository. Read
it before you change anything. Read `docs/DESIGN.md` sections 1 to 4
before you write code.

## 1. What this repository is

`rf-agent`: a small program that runs beside a member's SDR, reads the
decoder they already run, thins what it hears, and publishes it over
MQTT to the community broker. Sonoran stores it and draws it on its RF
tab. Sonoran is a separate repository,
`ArizonaMeshtasticCommunity/beta-sonoran`.

| Lives here | Lives in Sonoran |
|---|---|
| The agent, its readers, thinning, heartbeat | `sonoran-rf-ingest`, the `rf_*` tables, `/api/rf`, the RF tab |
| The `rf-sweep` spectrum container (Phase 2) | Storage, retention, dedup across stations |
| Design sections 3 and 4 | Design sections 5 to 7 |
| A copy of the contract | The source of the contract, `sonoran/rf/schema.py` |

## 2. The contract

`schema/rf-message-v1.schema.json` is what every message must satisfy.
It is generated from Sonoran's `sonoran/rf/schema.py`. Never edit it by
hand here.

To change the contract:

1. Change `sonoran/rf/schema.py` in Sonoran, and regenerate its
   `docs/rf-message-v1.schema.json`. Sonoran has a test that the two
   match.
2. Copy that file to `schema/` here, in its own commit.
3. Check `examples/` still pass `tools/validate.py`.

A change that an older Sonoran would reject is a new version, `v: 2`, on
`rf/v2/...` topics. Adding an optional field is not: unknown fields are
ignored by design.

Every message the agent builds must pass `tools/validate.py`. The tests
check this, not only the examples.

## 3. Rules that are not negotiable

- **Local receivers only.** Never fetch from the internet: no ADS-B
  aggregator, no APRS-IS, no SondeHub, no TLE download. A point we did
  not receive is a claim we cannot check.
- **Never open an SDR.** Read the decoder's network output. A dongle can
  be opened by one program, and members feed other services from it.
  The only exception is `rf-sweep`, which owns a dongle chosen by serial
  number and does nothing else.
- **Thin at the edge** (design 4.3). ADS-B is one point per aircraft per
  `ADSB_INTERVAL_S`. Count what is thinned and send it in the heartbeat.
- **Privacy** (design 8). Round the station position to
  `STATION_PRECISION_M` before it leaves the site. `ISM_ALLOW` is empty by
  default, so no `rtl_433` device passes until a member names it.
- **No listening ports, no privileges.** The agent makes outgoing
  connections: to its decoders and to the broker, over TLS. The one
  exception is a decoder that only broadcasts and accepts no connection,
  `radiosonde_auto_rx` and `rtl_433`. Those are read from a UDP socket,
  which serves nothing and accepts no connection, and every datagram is
  treated as untrusted input. Nothing else binds a port.
- **Publish to the station's own topic only**, `rf/v1/<station>/...`. The
  broker enforces it; the agent must not rely on that.
- **Never exit on bad input.** A bad frame is counted and skipped. A
  decoder or broker that goes away is retried with backoff.

## 4. Phase 1, the first build

Aircraft only (design 9):

1. Readers for readsb Beast (TCP 30005) or `aircraft.json`, and for
   dump978 JSON (TCP 30979).
2. Normalise to the batch in the contract. ICAO address in lower case.
3. Thinning per aircraft, plus the first and last point of each visit.
4. Batches of at most 100 observations, QoS 0.
5. The heartbeat every 60 s, retained, QoS 1, with the counts.
6. Configuration from environment variables only (README).
7. A Docker image, `ghcr.io/arizonameshtasticcommunity/rf-agent`.

The language is Python 3.13 with `aiomqtt`. It matches Sonoran and lets
the tests reuse `tools/validate.py` directly. The package is
`src/rf_agent`: one module per reader, `thinning.py`, `publisher.py`.

### Phase 2, the 900 MHz band

`rf-sweep` (`src/rf_agent/sweep.py`, `Dockerfile.sweep`) runs `rtl_power`
on a dongle chosen by serial, at a fixed gain, and serves its lines on
TCP. The agent reads them with `spectrum=tcp://...` and sends one
summary a minute (`src/rf_agent/spectrum.py`). The agent still never
opens a device.

### Phases 3 and 4, APRS, radiosondes and sensors

`src/rf_agent/aprs.py` reads direwolf's KISS stream over TCP and decodes
AX.25, then APRS positions in all three shapes: uncompressed, compressed
and Mic-E. A packet with no position is skipped, and so are objects and
items, which are things a station places rather than the station itself.

`src/rf_agent/sonde.py` reads `radiosonde_auto_rx` summaries and
`src/rf_agent/ism.py` reads `rtl_433`, both over UDP. A sensor transmits
no position, so its point carries the station's **rounded** position:
it says where it was heard, not where it is. `ISM_ALLOW` is empty by
default and Sonoran applies the same list again.

## 5. Testing

- Readers are tested with decoder output: a Beast capture, an
  `aircraft.json` file, dump978 lines. Record from a real receiver and
  shift every position with `tools/shift_capture.py` before it is
  committed, so no real station is in the repository. Until a recording
  is committed, `tests/fixtures/make_captures.py` writes the captures,
  for invented aircraft, in each decoder's exact format.
- Spectrum summaries are tested with an `rtl_power` capture: a steady
  carrier must land in its bin, and a bursty one must raise its `busy`.
- APRS is tested against the examples in the APRS 1.01 specification,
  and Mic-E against an encoder in the fixtures, since its format is not
  written out there in a form a test can use directly.
- Thinning has unit tests that count points in and out.
- Every message a test builds is checked against the schema.
- Do not run heavy tests on a machine that also serves production.

## 6. Writing

- Documentation and comments use short, plain sentences, one idea each,
  as Sonoran does.
- **No system details in the repository.** No hostnames, IP addresses,
  ports of a real deployment, account names, container names or
  passwords. Examples use `broker.example`.

## 7. Commits

- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`.
- Author and committer: `Logan <39987450+logans-stuff@users.noreply.github.com>`.
- No AI attribution anywhere: no `Co-Authored-By` for an assistant, no
  "Generated with" line, in commits or pull requests.
- One pull request for a batch of related work, not one per change.
