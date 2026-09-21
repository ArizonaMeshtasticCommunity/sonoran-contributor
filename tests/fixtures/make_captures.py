"""Write the test captures: Beast, aircraft.json snapshots, dump978 lines.

    uv run python tests/fixtures/make_captures.py

The captures are built, not recorded: every aircraft here is invented,
flying round a point in empty desert where no station stands. They are
in each decoder's exact output format, with the noise a real feed has.
When a real recording replaces one, shift it first with
`tools/shift_capture.py`, so no real station is in the repository.

The output is deterministic. Run it again and git shows no change.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from rf_agent.beast import Frame, encode_frame

from encode import (
    es,
    gnss_position_me,
    ident_me,
    identity_reply,
    position_me,
    surface_me,
    velocity_me,
)

HERE = Path(__file__).parent
# A fictional site. Tests that need a station position use this one.
SITE = (32.80, -113.30)
START = datetime(2026, 9, 22, 18, 0, 0, tzinfo=UTC).timestamp()
TICKS = 12_000_000


def moved(lat: float, lon: float, kn: float, track: float, seconds: float) -> tuple[float, float]:
    metres = kn * 1852 / 3600 * seconds
    dlat = metres * math.cos(math.radians(track)) / 111_320
    dlon = metres * math.sin(math.radians(track)) / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def beast() -> bytes:
    frames: list[tuple[float, int, bytes]] = []  # (seconds, type, message)

    def airborne(icao, start, kn, track, feet, spans, callsign=None, df18=None, gnss=None):
        for first, last in spans:
            t = first
            n = 0
            while t < last:
                lat, lon = moved(*start, kn, track, t)
                me = (
                    gnss_position_me(lat, lon, n % 2 == 1, gnss)
                    if gnss
                    else position_me(lat, lon, n % 2 == 1, feet)
                )
                frames.append((t, 0x33, es(icao, me, df18)))
                if n % 2 == 0:
                    frames.append((t + 0.2, 0x33, es(icao, velocity_me(kn, track), df18)))
                if callsign and n % 10 == 0:
                    frames.append((t + 0.3, 0x33, es(icao, ident_me(callsign), df18)))
                t += 0.5
                n += 1

    # Eastbound, with a 0x1a in its address, so its frames carry escapes.
    airborne("a11a2b", (32.80, -113.72), 250, 90, 10_000, [(0, 200)], "TST101")
    for s in range(1, 200, 3):
        frames.append((s + 0.1, 0x32, identity_reply("a11a2b", "1200")))
    # Northbound, heard twice with a gap longer than a visit.
    airborne("c0ffee", (32.55, -113.20), 420, 0, 35_000, [(0, 50), (130, 200)], "TST202")
    # A non-ICAO address (DF18, CF 1), with a GNSS height.
    airborne("abc123", (32.90, -113.40), 90, 225, 0, [(20, 80)], df18=1, gnss=1500)
    # Taxiing near the site.
    for n in range(60):
        lat, lon = moved(32.79, -113.31, 15, 45, n)
        frames.append((n + 0.4, 0x33, es("a0b0c0", surface_me(lat, lon, n % 2 == 1, 28, 45))))
    # The noise of a real feed.
    for s in range(0, 200, 7):
        bad = bytearray(es("a11a2b", velocity_me(250, 90)))
        bad[6] ^= 0x10
        frames.append((s + 0.05, 0x33, bytes(bad)))  # bad parity
        frames.append((s + 0.06, 0x31, bytes.fromhex("0a2c")))  # Mode A/C
        frames.append((s + 0.07, 0x32, bytes.fromhex("5da11a2b123456")))  # DF11

    frames.sort(key=lambda f: f[0])
    out = bytearray()
    for n, (t, kind, message) in enumerate(frames):
        signal = 40 + (n * 37) % 160
        out += encode_frame(Frame(kind, 5_000_000 + round(t * TICKS), signal, message))
        if n % 97 == 0:
            out += b"\x00\xff\x13"  # garbage between frames
        if n % 131 == 0:
            out += b"\x1a\x34" + bytes(20)  # a frame type the agent skips
    return bytes(out)


def aircraft_json() -> str:
    lines = []
    for i in range(90):
        now = START + i
        aircraft = []

        lat, lon = moved(32.70, -113.50, 180, 60, i)
        aircraft.append(
            {"hex": "a4b5c6", "type": "adsb_icao", "flight": "TST303 ", "alt_baro": 12000,
             "alt_geom": 12250, "gs": 180.2, "track": 60.1, "squawk": "4521",
             "category": "A1", "lat": round(lat, 6), "lon": round(lon, 6), "seen_pos": 0.2,
             "seen": 0.1, "rssi": -18.4}
        )  # fmt: skip
        # Its position was computed by an MLAT server, not heard here.
        aircraft.append(
            {"hex": "a7a7a7", "type": "mlat", "alt_baro": 8000, "lat": 32.9, "lon": -113.1,
             "seen_pos": 0.5, "mlat": ["lat", "lon"], "rssi": -30.0}
        )  # fmt: skip
        # dump1090-fa marks MLAT by field, and uses older names.
        aircraft.append(
            {"hex": "a55555", "altitude": 9000, "speed": 200, "lat": 32.95, "lon": -113.0,
             "seen_pos": 0.4, "mlat": ["lat", "lon", "track"], "rssi": -28.0}
        )  # fmt: skip
        # A TIS-B target, with a non-ICAO address.
        lat, lon = moved(32.60, -113.10, 100, 300, i)
        aircraft.append(
            {"hex": "~2b3c4d", "type": "tisb_other", "alt_baro": 4500, "gs": 100,
             "track": 300, "lat": round(lat, 6), "lon": round(lon, 6), "seen_pos": 1.0,
             "rssi": -25.0}
        )  # fmt: skip
        # On the ground.
        aircraft.append(
            {"hex": "a9c8d7", "type": "adsb_icao", "alt_baro": "ground", "gs": 12,
             "lat": 32.801, "lon": -113.299, "seen_pos": 0.3, "rssi": -12.0}
        )  # fmt: skip
        # No position at all.
        aircraft.append({"hex": "adadad", "type": "adsb_icao", "alt_baro": 30000, "seen": 1.0})
        # A position heard every third second: the snapshots between repeat it.
        last_fix = i - i % 3
        aircraft.append(
            {"hex": "abcdef", "type": "adsb_icao_nt", "alt_baro": 20000, "lat": 32.7,
             "lon": -113.2 + last_fix * 0.001, "seen_pos": round(i - last_fix + 0.1, 1),
             "rssi": -22.0}
        )  # fmt: skip
        # dump1090-fa, older fields, a real position.
        aircraft.append(
            {"hex": "a3a3a3", "altitude": 6000, "speed": 140, "track": 10, "lat": 32.5,
             "lon": -113.6 + i * 0.0005, "seen_pos": 0.0, "rssi": -20.0}
        )  # fmt: skip
        doc = {"now": now, "messages": 1000 + i * 40, "aircraft": aircraft}
        lines.append(json.dumps(doc, separators=(",", ":")))
    return "\n".join(lines) + "\n"


def dump978() -> str:
    lines = []
    for i in range(90):
        t = START + i
        lat, lon = moved(32.75, -113.45, 95, 120, i)
        msg = {
            "address": "a6b7c8",
            "address_qualifier": "adsb_icao",
            "airground_state": "airborne",
            "position": {"lat": round(lat, 5), "lon": round(lon, 5)},
            "pressure_altitude": 5500,
            "geometric_altitude": 5725,
            "ground_speed": 95,
            "true_track": 120.2,
            "nic": 8,
            "metadata": {"received_at": round(t + 0.25, 3), "rssi": -19.5, "errors": 0},
        }
        if i % 5 == 0:
            msg["callsign"] = "N123TS"
            msg["emitter_category"] = "A1"
            msg["flightplan_id"] = "1200"
        lines.append(json.dumps(msg, separators=(",", ":")))

        lat, lon = moved(32.85, -113.00, 110, 250, i)
        lines.append(
            json.dumps(
                {"address": "1a2b3c", "address_qualifier": "tisb_trackfile",
                 "position": {"lat": round(lat, 5), "lon": round(lon, 5)},
                 "geometric_altitude": 3000, "ground_speed": 110, "true_track": 250,
                 "metadata": {"received_at": round(t + 0.5, 3), "rssi": -27.0}},
                separators=(",", ":"),
            )
        )  # fmt: skip
        if i % 10 == 0:
            # A ground vehicle: heard, but not an aircraft.
            lines.append(
                json.dumps(
                    {"address": "a0a0a0", "address_qualifier": "vehicle",
                     "position": {"lat": 32.8, "lon": -113.3},
                     "metadata": {"received_at": round(t + 0.7, 3)}},
                    separators=(",", ":"),
                )
            )  # fmt: skip
            # Only the mode status half: a callsign, no position.
            lines.append(
                json.dumps(
                    {"address": "a6b7c8", "address_qualifier": "adsb_icao", "callsign": "N123TS",
                     "metadata": {"received_at": round(t + 0.8, 3)}},
                    separators=(",", ":"),
                )
            )  # fmt: skip
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    (HERE / "beast.bin").write_bytes(beast())
    (HERE / "aircraft.jsonl").write_text(aircraft_json())
    (HERE / "dump978.jsonl").write_text(dump978())
