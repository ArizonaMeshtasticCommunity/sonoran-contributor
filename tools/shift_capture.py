"""Move a recorded capture somewhere else, before it is committed.

    uv run python tools/shift_capture.py beast IN OUT --dlat D --dlon D --station LAT,LON
    uv run python tools/shift_capture.py aircraft-json IN OUT --dlat D --dlon D [--dt S]
    uv run python tools/shift_capture.py dump978 IN OUT --dlat D --dlon D [--dt S]

Test captures are recorded from a real receiver and every position is
moved, so no real station is in the repository (AGENTS.md 5).

- `beast`: each position frame is decoded, moved, and encoded again,
  with the same even or odd format and new parity. A frame whose
  position cannot be moved is dropped rather than kept: the first half of
  a CPR pair, a surface frame, a TIS-B or ADS-R rebroadcast, a frame
  with bad parity, and Comm-B replies (DF20, DF21), which can carry one.
  `--station` is where the capture was recorded, used to decode surface
  frames. It is never written out.
- `aircraft-json`: one `aircraft.json` document per line. Moves every
  aircraft's `lat`, `lon`, `lastPosition` and `rr_lat`/`rr_lon`.
- `dump978`: dump978-fa JSON lines. Moves `position`.

`--dt` moves the JSON captures' times by that many seconds. A shift hides
the site from a reader of the repository. Addresses, callsigns and
times still name real flights, so move the times too, and choose the
shift and the times freely rather than by a rule.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rf_agent.beast import BeastFramer, Frame, Tracker, encode_frame
from rf_agent.modes import address_of, cpr_encode, crc, parity_ok

POSITION_TCS = set(range(5, 19)) | {20, 21, 22}
LOW_34 = (1 << 34) - 1


def shift_beast(data: bytes, dlat: float, dlon: float, station: tuple[float, float] | None):
    """Return the moved capture, and how many frames were dropped."""
    tracker = Tracker("adsb", station)
    out = bytearray()
    dropped = 0
    for frame in BeastFramer().feed(data):
        msg = frame.message
        df = msg[0] >> 3
        if frame.kind == 0x33 and df in (20, 21):
            dropped += 1
            continue
        if frame.kind != 0x33 or df not in (17, 18):
            tracker.frame(frame, frame.ticks / 12e6)
            out += encode_frame(frame)
            continue
        me = int.from_bytes(msg[4:11])
        is_position = (me >> 51) in POSITION_TCS
        if not parity_ok(msg) or (is_position and address_of(msg) is None):
            dropped += 1
            continue
        point = tracker.frame(frame, frame.ticks / 12e6)
        if not is_position:
            out += encode_frame(frame)
            continue
        if point is None:
            dropped += 1
            continue
        surface = (me >> 51) <= 8
        odd = bool((me >> 34) & 1)
        ylat, xlon = cpr_encode(point.lat + dlat, point.lon + dlon, odd, surface)
        me = (me & ~LOW_34) | (ylat << 17) | xlon
        head = msg[:4] + me.to_bytes(7)
        moved = Frame(frame.kind, frame.ticks, frame.signal, head + crc(head).to_bytes(3))
        out += encode_frame(moved)
    return bytes(out), dropped


def _move(obj: dict[str, Any], lat_key: str, lon_key: str, dlat: float, dlon: float) -> None:
    if isinstance(obj.get(lat_key), int | float) and isinstance(obj.get(lon_key), int | float):
        obj[lat_key] = round(obj[lat_key] + dlat, 6)
        obj[lon_key] = round(((obj[lon_key] + dlon + 180) % 360) - 180, 6)


def shift_aircraft_json(line: str, dlat: float, dlon: float, dt: float) -> str:
    doc = json.loads(line)
    if isinstance(doc.get("now"), int | float):
        doc["now"] += dt
    for ac in doc.get("aircraft", []):
        _move(ac, "lat", "lon", dlat, dlon)
        _move(ac, "rr_lat", "rr_lon", dlat, dlon)
        if isinstance(ac.get("lastPosition"), dict):
            _move(ac["lastPosition"], "lat", "lon", dlat, dlon)
    return json.dumps(doc, separators=(",", ":"))


def shift_dump978(line: str, dlat: float, dlon: float, dt: float) -> str:
    msg = json.loads(line)
    if isinstance(msg.get("position"), dict):
        _move(msg["position"], "lat", "lon", dlat, dlon)
    meta = msg.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("received_at"), int | float):
        meta["received_at"] += dt
    return json.dumps(msg, separators=(",", ":"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("format", choices=["beast", "aircraft-json", "dump978"])
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--dlat", type=float, required=True)
    parser.add_argument("--dlon", type=float, required=True)
    parser.add_argument("--dt", type=float, default=0.0, help="seconds to move JSON times by")
    parser.add_argument("--station", help="LAT,LON where it was recorded (beast only)")
    args = parser.parse_args(argv)

    if args.format == "beast":
        station = tuple(float(x) for x in args.station.split(",")) if args.station else None
        data, dropped = shift_beast(args.source.read_bytes(), args.dlat, args.dlon, station)
        args.target.write_bytes(data)
        print(f"wrote {args.target}; dropped {dropped} frames whose position could not be moved")
        return 0

    shift = shift_aircraft_json if args.format == "aircraft-json" else shift_dump978
    lines = [
        shift(line, args.dlat, args.dlon, args.dt)
        for line in args.source.read_text().splitlines()
        if line.strip()
    ]
    args.target.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.target}: {len(lines)} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
