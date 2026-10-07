# Writing your own mod

A **mod** is a small program you write that reads your own receiver and
sends what it hears through your agent, as your station's data. Use it
for a decoder we do not support, an experiment, or a sensor of your own.
Sonoran shows it on the RF page in its own **Member mods** layer.

You need no broker login of your own. The agent you already run takes
your points, checks them, and sends them on your login, beside your
heartbeat.

## 1. Turn on the input

Add a `custom` source to the agent:

```yaml
      SOURCES: adsb=beast://readsb:30005,custom=udp://127.0.0.1:30960
```

The agent receives on that UDP port. It accepts no connection and serves
nothing; it only reads what arrives. `127.0.0.1` is this machine only.
For a mod in another container of the same compose file, use
`custom=udp://0.0.0.0:30960` and do **not** publish the port on the host.

## 2. Send a point

One JSON object per line. A UDP datagram may hold one line or several.

```json
{"mod": "lightning", "id": "strike-1", "t": 1759600000.5, "heard_here": true, "meta": {"distance_km": 12, "energy": 3150}}
```

| Field | Meaning |
|-------|---------|
| `mod` | Your mod's name: `a-z`, `0-9` and `-`, up to 16 characters. Required. |
| `id` | The thing's id within your mod: `a-z`, `0-9`, `_`, `.` and `-`, up to 32 characters, starting with a letter or digit. Upper case is made lower. Required. |
| `t` | When you heard it: seconds since the epoch, or an RFC 3339 time. Without it, the time the agent got the line. |
| `lat`, `lon` | Where the thing is, both or neither. |
| `heard_here` | `true` when the thing has no position of its own: it is drawn at your station's rounded position, and any `lat`/`lon` is not used. |
| `meta` | Up to 16 values: numbers, `true`/`false`, or text of at most 256 characters. Keys look like `distance_km`. Anything else is left out. |

A point with neither a position nor `heard_here` is listed in the table
and never drawn on the map.

From a shell:

```sh
echo '{"mod": "lightning", "id": "strike-1", "heard_here": true}' | nc -u -w1 127.0.0.1 30960
```

[`examples/mod_stdin.py`](../examples/mod_stdin.py) is a complete mod: it
reads JSON lines from a decoder on its standard input, picks the fields
you name, and sends each one to the agent.

## 3. The library

`rf_agent.messages` needs nothing but the Python standard library. If you
would rather check your points before you send them, import it:

```python
from rf_agent.messages import custom_point

point = custom_point("strike-1", time.time(), heard_here=True, meta={"distance_km": 12})
if point is None:
    ...  # not a point the contract allows
```

`custom_point` returns the point as the contract spells it, or None.
`custom_batch` builds the whole message the agent publishes, if you want
to see one. Your mod never needs to copy the agent's code.

## 4. Units

Sonoran shows each value with its key, exactly as you sent it. It adds a
unit only from a fixed list, by how the key ends, and only to numbers:

| Key ends with | Shown as |
|---------------|----------|
| `_db`, `_dbm` | dB, dBm |
| `_hz`, `_khz`, `_mhz` | Hz, kHz, MHz |
| `_c` | °C |
| `_pct` | % |
| `_m`, `_km` | m, km |
| `_v` | V |
| `_s` | s |

## 5. What the agent does with it

- **Checks** each line. A line that is not a point is counted and
  dropped, never sent.
- **Thins**: one point per thing per `CUSTOM_INTERVAL_S` (10 s).
- **Limits** each mod to `CUSTOM_PER_MINUTE` points (600), and the agent
  to 16 mods.
- **Sends** them in batches of up to 100 on `rf/v1/<station>/custom`.

## 6. What Sonoran does with it

Sonoran does not trust any station, and a mod gets no more than a
built-in decoder. It checks every point again, holds each station to
its own limits, and keeps a mod's points under your station and your mod,
so no mod can post as another station, another mod or a built-in kind.

On the page:

- Every value is plain text, never a link, an image, a colour or markup.
  Long values are shortened, with the whole value one click away.
- Every mod is drawn with the same plain mark, unlike any built-in kind.
- Every point says "From a member's mod: `<mod>` at `<station>`", and
  that it is unverified.
- The layer is off until a reader turns it on, with a switch for each
  station and each mod.
- Your mod's time is shown beside the time Sonoran received the point,
  so a wrong clock shows.

An operator can hide a mod, or quarantine a station, at once. You can ask
for your mod's data to be deleted; revoking your login stops it at once.

## 7. The rules

The same as for everything else this agent sends:

- **Local receivers only.** Send what your own receiver heard. Never
  data fetched from the internet: a point we did not receive is a claim
  we cannot check.
- **Privacy.** Round positions that are people's. Never send what
  somebody said or typed.
- **Never take a dongle that feeds something else.**
