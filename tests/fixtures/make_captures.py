"""Write the test captures: Beast, aircraft.json, dump978 lines, rtl_power sweeps.

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
import random
from datetime import UTC, datetime
from pathlib import Path

from rf_agent.beast import Frame, encode_frame

from encode import (
    aprs_compressed,
    ax25,
    es,
    gnss_position_me,
    ident_me,
    identity_reply,
    kiss_frame,
    mic_e,
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


# The rtl_power capture: two minutes of the 902 to 928 MHz band.
SWEEP_STEP = 48828.125  # rtl_power's bin, a little under the 50 kHz asked for
SWEEP_HOP_BINS = 40
NOISE_DB = -50.0
STEADY_HZ = 915_000_000  # always on, 12 dB over the floor
BURST_HZ = 906_875_000  # on in 40% of sweeps, 20 dB over the floor
SWEEP_HEADER = "# rf-sweep range=902000000:928000000:50000 gain_db=32.8"


def rtl_power() -> str:
    rng = random.Random(915)
    lines = [SWEEP_HEADER]
    for n in range(24):  # one sweep every 5 s
        when = datetime.fromtimestamp(START + 5 * n, UTC)
        date, clock = when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S")
        burst = n % 5 in (1, 3)
        low = 902_000_000.0
        while low < 928_000_000:
            high = low + SWEEP_HOP_BINS * SWEEP_STEP
            levels = []
            for i in range(SWEEP_HOP_BINS):
                f = low + i * SWEEP_STEP
                db = NOISE_DB + rng.gauss(0, 1.0)
                if f <= STEADY_HZ < f + SWEEP_STEP:
                    db = NOISE_DB + 12 + rng.gauss(0, 0.3)
                if burst and f <= BURST_HZ < f + SWEEP_STEP:
                    db = NOISE_DB + 20 + rng.gauss(0, 0.3)
                levels.append(f"{db:.2f}")
            head = f"{date}, {clock}, {low:.0f}, {high:.0f}, {SWEEP_STEP:.2f}, 3000"
            lines.append(head + ", " + ", ".join(levels))
            low = high
    return "\n".join(lines) + "\n"


def aprs() -> bytes:
    """A direwolf KISS stream: three stations, and packets with no position."""
    frames: list[bytes] = []
    # A fixed station, uncompressed, with an altitude and a comment.
    for _ in range(3):
        frames.append(ax25("KD7ABC", "APRS", b"!3248.00N/11318.00W-Shack, 240 m ASL /A=000787"))
    # A walker, compressed, moving north east.
    for n in range(5):
        lat, lon = moved(32.76, -113.34, 3, 45, n * 60)
        frames.append(ax25("W1XYZ-5", "APRS", aprs_compressed(lat, lon)))
    # A car, Mic-E, which is how most mobile stations report.
    for n in range(12):
        lat, lon = moved(32.84, -113.26, 55, 200, n * 30)
        dest, info = mic_e(lat, lon, 55, 200)
        frames.append(ax25("KD7ABC-9", dest, info))
    # Packets that carry no position at all.
    frames.append(ax25("N0CALL", "APRS", b":KD7ABC   :thanks for the lift{001"))
    frames.append(ax25("KD7ABC", "APRS", b">Monitoring 146.520"))
    frames.append(ax25("KD7ABC", "APRS", b"T#005,199,000,255,073,123,01101001"))
    # An object a station places on the map: not the station, so skipped.
    frames.append(ax25("KD7ABC", "APRS", b";FIELD DAY*111111z3248.00N/11318.00W-tent"))
    # A comment holding the bytes KISS has to escape.
    frames.append(ax25("W1XYZ-5", "APRS", b"!3245.00N/11320.00W-edge \xc0 and \xdb here"))

    out = bytearray()
    for n, frame in enumerate(frames):
        out += kiss_frame(frame)
        if n % 7 == 0:
            out += b"\xc0"  # an empty frame between two others
        if n % 11 == 0:
            out += b"junk not a frame"
    return bytes(out)


def sonde() -> str:
    """radiosonde_auto_rx summaries, one datagram per line."""
    lines = []
    for n in range(50):
        lat, lon = moved(32.90, -113.10, 25, 70, n)
        lines.append(
            json.dumps(
                {"type": "PAYLOAD_SUMMARY", "callsign": "S4132077",
                 "latitude": round(lat, 5), "longitude": round(lon, 5),
                 "altitude": 1200 + n * 55, "speed": 46.3, "heading": 70.0,
                 "model": "RS41", "freq": "404.100 MHz", "freq_float": 404.1,
                 "sats": 9, "batt": 2.8, "snr": 14.2, "temp": -12.4,
                 "time": f"18:0{n // 10}:{n % 60:02d}", "comment": "RS41 404.100 MHz"},
                separators=(",", ":"),
            )
        )  # fmt: skip
        if n % 20 == 0:
            # auto_rx also announces itself; not a payload.
            lines.append(json.dumps({"type": "STATION", "callsign": "TEST-RX"}))
    lines.append("{not json")
    return "\n".join(lines) + "\n"


def rtl433() -> str:
    """rtl_433 devices, one datagram per line, bare and syslog wrapped."""
    lines = []
    for n in range(20):
        reading = {"time": f"2026-09-22 18:{n:02d}:00", "model": "Acurite-Tower", "id": 1234,
                   "channel": "A", "battery_ok": 1, "temperature_C": 21.5 + n * 0.1,
                   "humidity": 44, "mic": "CHECKSUM", "rssi": -11.2}  # fmt: skip
        line = json.dumps(reading, separators=(",", ":"))
        # rtl_433 wraps its JSON in a syslog line when sent with -F syslog.
        lines.append(f"<165>1 2026-09-22T18:{n:02d}:00Z rtl433 - - - {line}" if n % 4 else line)
        if n % 5 == 0:
            # A model nobody allow-listed: a neighbour's tyre sensor.
            lines.append(
                json.dumps({"time": "2026-09-22 18:00:00", "model": "Schrader-TPMS",
                            "id": 998877, "pressure_kPa": 240}, separators=(",", ":"))
            )  # fmt: skip
    # A device with a channel but no id.
    lines.append(
        json.dumps({"model": "Acurite-Tower", "channel": "B", "temperature_C": 19.0},
                   separators=(",", ":"))
    )  # fmt: skip
    lines.append("not json at all")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    (HERE / "beast.bin").write_bytes(beast())
    (HERE / "aircraft.jsonl").write_text(aircraft_json())
    (HERE / "dump978.jsonl").write_text(dump978())
    (HERE / "rtl_power.csv").write_text(rtl_power())
    (HERE / "aprs.kiss").write_bytes(aprs())
    (HERE / "sonde.jsonl").write_text(sonde())
    (HERE / "rtl433.jsonl").write_text(rtl433())
