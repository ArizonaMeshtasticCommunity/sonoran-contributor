"""Read direwolf's KISS stream: AX.25 frames in, APRS positions out.

direwolf serves KISS on TCP 8001 and takes several readers at once, so
the agent connects out and takes nothing from anyone else.

Three layers, in order:

1. **KISS** frames a byte stream: `0xc0` delimits, `0xdb` escapes.
2. **AX.25** carries the callsigns and the payload. Only UI frames with
   no layer 3 protocol (control `0x03`, PID `0xf0`) carry APRS.
3. **APRS** puts the position in the payload, in one of three shapes:
   uncompressed (`4903.50N/07201.75W`), compressed (base 91), or Mic-E,
   which hides half the position in the destination callsign.

A packet with no position is skipped: messages, status, telemetry and
bulletins say nothing about where a station is. Objects and items are
skipped too, because they are things a station places on the map rather
than the station itself.

APRS is public by design and is stored as heard (design 8). The
identity is the callsign with its SSID, which is how APRS names a
station.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from rf_agent.messages import Observation, observation

logger = logging.getLogger(__name__)

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
UI_CONTROL, NO_LAYER3 = 0x03, 0xF0
MAX_FRAME = 1 << 12

# Position reports. `!` and `=` carry no time, `/` and `@` carry seven
# characters of it, which we ignore: the receiver's clock is the one we
# can place (design 5.3).
NO_TIME = "!="
WITH_TIME = "/@"
MIC_E = "`'\x1c\x1d"

UNCOMPRESSED = re.compile(
    rb"^(\d{2})(\d{2}\.\d{2})([NS])(.)(\d{3})(\d{2}\.\d{2})([EW])(.)", re.DOTALL
)
COURSE_SPEED = re.compile(rb"^(\d{3})/(\d{3})")
ALTITUDE = re.compile(rb"/A=(-?\d{6})")


class KissFramer:
    """Cut a KISS stream into frames. Counts what it had to skip."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.skipped = 0

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buf += data
        while True:
            start = self._buf.find(FEND)
            if start < 0:
                if len(self._buf) > MAX_FRAME:
                    self.skipped += 1
                    self._buf.clear()
                return
            end = self._buf.find(FEND, start + 1)
            if end < 0:
                del self._buf[:start]
                if len(self._buf) > MAX_FRAME:
                    self.skipped += 1
                    self._buf.clear()
                return
            raw = bytes(self._buf[start + 1 : end])
            # Keep the closing delimiter: two frames may share one.
            del self._buf[:end]
            if not raw:
                continue  # back to back delimiters
            frame = self._unescape(raw)
            # The first byte is the port and command; only data frames
            # (command 0) carry a packet.
            if frame and frame[0] & 0x0F == 0:
                yield frame[1:]
            else:
                self.skipped += 1

    @staticmethod
    def _unescape(raw: bytes) -> bytes:
        if FESC not in raw:
            return raw
        out = bytearray()
        escaped = False
        for byte in raw:
            if escaped:
                out.append(FEND if byte == TFEND else FESC if byte == TFESC else byte)
                escaped = False
            elif byte == FESC:
                escaped = True
            else:
                out.append(byte)
        return bytes(out)


def _callsign(address: bytes) -> str:
    """Decode one AX.25 address: six shifted characters and an SSID."""
    call = "".join(chr(byte >> 1) for byte in address[:6]).strip()
    ssid = (address[6] >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


@dataclass(frozen=True, slots=True)
class Packet:
    source: str
    destination: str
    info: bytes


def parse_ax25(frame: bytes) -> Packet | None:
    """Return the callsigns and payload of a UI frame, or None."""
    if len(frame) < 16:
        return None
    addresses = []
    at = 0
    while at + 7 <= len(frame):
        field = frame[at : at + 7]
        addresses.append(field)
        at += 7
        if field[6] & 1:  # the last address sets the extension bit
            break
        if len(addresses) > 10:
            return None
    if len(addresses) < 2 or at + 2 > len(frame):
        return None
    if frame[at] != UI_CONTROL or frame[at + 1] != NO_LAYER3:
        return None  # not an APRS-carrying frame
    return Packet(_callsign(addresses[1]), _callsign(addresses[0]), frame[at + 2 :])


def _degrees(degrees: bytes, minutes: bytes, hemisphere: bytes) -> float | None:
    try:
        value = int(degrees) + float(minutes) / 60
    except ValueError:
        return None
    return -value if hemisphere in (b"S", b"W") else value


def _base91(text: bytes) -> int:
    value = 0
    for byte in text:
        if not 33 <= byte <= 123:
            raise ValueError("not base 91")
        value = value * 91 + (byte - 33)
    return value


def _extension(rest: bytes) -> tuple[float | None, float | None]:
    """Course and speed, when the comment starts with `090/036`."""
    match = COURSE_SPEED.match(rest)
    if match is None:
        return None, None
    course = int(match.group(1))
    return (course % 360 if course else None), float(match.group(2))


def _altitude_m(info: bytes) -> float | None:
    match = ALTITUDE.search(info)
    return int(match.group(1)) * 0.3048 if match else None


def parse_position(info: bytes) -> dict[str, Any] | None:
    """Return the position a packet reports, or None when it reports none."""
    if not info:
        return None
    kind = chr(info[0])
    if kind in MIC_E:
        return None  # Mic-E needs the destination too; see `parse_mic_e`
    if kind not in NO_TIME and kind not in WITH_TIME:
        return None
    body = info[8:] if kind in WITH_TIME else info[1:]
    if not body:
        return None

    match = UNCOMPRESSED.match(body)
    if match is not None:
        lat = _degrees(match.group(1), match.group(2), match.group(3))
        lon = _degrees(match.group(5), match.group(6), match.group(7))
        if lat is None or lon is None:
            return None
        rest = body[match.end() :]
        course, speed = _extension(rest)
        return {
            "lat": lat,
            "lon": lon,
            "course": course,
            "speed_kn": speed,
            "alt_m": _altitude_m(info),
            "symbol": chr(match.group(4)[0]) + chr(match.group(8)[0]),
        }

    # Compressed: a symbol table character, four characters of latitude,
    # four of longitude, the symbol, then course and speed.
    if len(body) >= 13 and not body[0:1].isdigit():
        try:
            lat = 90 - _base91(body[1:5]) / 380926
            lon = -180 + _base91(body[5:9]) / 190463
        except ValueError:
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        course = speed = None
        c, s = body[10], body[11]
        if 33 <= c <= 122 and c != 32:
            if chr(c) == "{":  # radio range, not a course
                course = speed = None
            else:
                course = ((c - 33) * 4) % 360
                speed = 1.08 ** (s - 33) - 1
        return {
            "lat": lat,
            "lon": lon,
            "course": course,
            "speed_kn": speed,
            "alt_m": _altitude_m(info),
            "symbol": chr(body[0]) + chr(body[9]),
        }
    return None


def parse_mic_e(destination: str, info: bytes) -> dict[str, Any] | None:
    """Decode Mic-E, whose latitude hides in the destination callsign.

    Each destination character carries a latitude digit and a message
    bit. The information field carries the longitude, the speed and the
    course, offset by 28 so they stay printable.
    """
    call = destination.split("-")[0]
    if len(call) != 6 or len(info) < 9:
        return None

    digits = ""
    for char in call:
        if "0" <= char <= "9":
            digits += char
        elif "A" <= char <= "J":
            digits += chr(ord(char) - ord("A") + ord("0"))
        elif "P" <= char <= "Y":
            digits += chr(ord(char) - ord("P") + ord("0"))
        elif char in "KLZ":
            digits += "0"  # position ambiguity: the digit is not sent
        else:
            return None
    try:
        lat = int(digits[0:2]) + (int(digits[2:4]) + int(digits[4:6]) / 100) / 60
    except ValueError:
        return None
    if not call[3] >= "P":
        lat = -lat  # south

    body = info[1:]
    degrees = body[0] - 28
    if call[4] >= "P":
        degrees += 100
    if 180 <= degrees <= 189:
        degrees -= 80
    elif 190 <= degrees <= 199:
        degrees -= 190
    minutes = body[1] - 28
    if minutes >= 60:
        minutes -= 60
    hundredths = body[2] - 28
    if not 0 <= degrees <= 179 or not 0 <= minutes <= 59 or not 0 <= hundredths <= 99:
        return None
    lon = degrees + (minutes + hundredths / 100) / 60
    if call[5] >= "P":
        lon = -lon  # west

    speed = (body[3] - 28) * 10
    course_speed = body[4] - 28
    speed += course_speed // 10
    course = (course_speed % 10) * 100 + (body[5] - 28)
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {
        "lat": lat,
        "lon": lon,
        "course": float(course % 360) if course else None,
        "speed_kn": float(speed),
        "alt_m": _mic_e_altitude(body[6:]),
        "symbol": chr(body[7]) + chr(body[6]) if len(body) > 7 else None,
    }


def _mic_e_altitude(rest: bytes) -> float | None:
    """Mic-E puts altitude in the comment, as three base 91 characters."""
    at = rest.find(b"}")
    if at < 3:
        return None
    try:
        metres = _base91(rest[at - 3 : at]) - 10000
    except ValueError:
        return None
    return float(metres) if -500 <= metres <= 60000 else None


class Direwolf:
    """Turn direwolf's KISS stream into APRS observations."""

    def __init__(self, kind: str = "aprs") -> None:
        self.kind = kind
        self.frames = KissFramer()
        self.bad = 0
        self.no_position = 0

    def feed(self, data: bytes, now: float) -> list[Observation]:
        out: list[Observation] = []
        for frame in self.frames.feed(data):
            try:
                point = self.frame(frame, now)
            except Exception:
                self.bad += 1
                continue
            if point is not None:
                out.append(point)
        return out

    def frame(self, frame: bytes, now: float) -> Observation | None:
        packet = parse_ax25(frame)
        if packet is None:
            self.bad += 1
            return None
        if packet.info[:1].decode("latin-1") in MIC_E:
            found = parse_mic_e(packet.destination, packet.info)
        else:
            found = parse_position(packet.info)
        if found is None:
            self.no_position += 1
            return None

        speed = found.get("speed_kn")
        point = observation(
            self.kind,
            packet.source,
            now,
            found["lat"],
            found["lon"],
            alt_m=found.get("alt_m"),
            alt_ref="geom" if found.get("alt_m") is not None else None,
            gs_kn=speed if speed else None,
            track_deg=found.get("course"),
            meta={
                "symbol": found.get("symbol"),
                "destination": packet.destination,
                "comment": _comment(packet.info),
            },
        )
        if point is None:
            self.bad += 1
        return point


def _comment(info: bytes) -> str | None:
    """The tail a station types, trimmed to something worth sending."""
    text = info.decode("latin-1", "replace").strip()
    text = "".join(char for char in text if char.isprintable())
    return text[-60:] or None
