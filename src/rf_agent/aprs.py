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

# A station may blank digits from the right to blur where it is, so the
# minutes are digits or spaces.
UNCOMPRESSED = re.compile(
    rb"^(\d{2})([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)", re.DOTALL
)
# How wide the blur is, in minutes, for each blanked digit count. One
# minute of latitude is 1852 m, so four blanked digits is about 111 km.
BLUR_MINUTES = {1: 0.1, 2: 1.0, 3: 10.0, 4: 60.0}
METRES_PER_MINUTE = 1852.0
OBJECT, ITEM, THIRD_PARTY = ";", ")", "}"
# An object or item name, with anything else collapsed to an underscore.
NAME_UNSAFE = re.compile(r"[^a-z0-9._+-]+")
COURSE_SPEED = re.compile(rb"^(\d{3})/(\d{3})")
# A station with no sensor sends dots or spaces in a reading's place,
# and that is not a zero: `.../...g...t...` measures no wind, gust or
# temperature.
WIND_OR_MISSING = re.compile(rb"^([\d. ]{3})/([\d. ]{3})")
# Base 91 telemetry between two pipes, and the DAO extension that adds a
# digit of precision to a position. Both sit inside the comment.
TELEMETRY = re.compile(rb"\|([!-{]{4,14})\|")
DAO = re.compile(rb"!([A-Za-z])([!-{ ])([!-{ ])!")
# What a weather station appends after its readings: the software and
# hardware that sent them, such as `.DsVP`. Not typed text.
MAX_WX_TYPE = 5
ALTITUDE = re.compile(rb"/A=(-?\d{6})")
# A station's own radio details, not a course and speed.
STATION_EXTENSION = re.compile(rb"^(PHG|RNG|DFS)[0-9A-Za-z]{4}")
# A weather station's readings, in the order APRS writes them.
# Each reading: how it is written, what it is called, what to multiply
# it by, and what to add. `l` is luminosity of 1000 and over, which APRS
# writes as a second letter rather than a fourth digit.
WEATHER = (
    (re.compile(rb"g([\d. ]{3})"), "gust_kn", 0.868976, 0),
    (re.compile(rb"t(-?[\d. ]{2,3})"), "temperature_f", 1.0, 0),
    (re.compile(rb"r([\d. ]{3})"), "rain_1h_in", 0.01, 0),
    (re.compile(rb"p([\d. ]{3})"), "rain_24h_in", 0.01, 0),
    (re.compile(rb"P([\d. ]{3})"), "rain_today_in", 0.01, 0),
    (re.compile(rb"h([\d. ]{2})"), "humidity", 1.0, 0),
    (re.compile(rb"b([\d. ]{5})"), "pressure_hpa", 0.1, 0),
    (re.compile(rb"L([\d. ]{3})"), "luminosity_wm2", 1.0, 0),
    (re.compile(rb"l([\d. ]{3})"), "luminosity_wm2", 1.0, 1000),
    # Snowfall is written in tenths of an inch, as aprslib reads it.
    (re.compile(rb"s([\d. ]{3})"), "snow_24h_in", 0.1, 0),
    (re.compile(rb"#([\d. ]{3})"), "rain_counter", 1.0, 0),
)
# The Mic-E message types, from the three message bits.
MIC_E_MESSAGES = (
    "Emergency", "Priority", "Special", "Committed",
    "Returning", "In Service", "En Route", "Off Duty",
)  # fmt: skip
WEATHER_SYMBOL = "_"


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
    # The digipeaters, each marked with `*` once it has repeated the
    # packet. Empty means the station reached us with nothing in between.
    path: tuple[str, ...] = ()

    @property
    def direct(self) -> bool:
        """True when no digipeater has repeated this packet.

        A relayed packet says where the station is, but says nothing
        about what this receiver can hear, so it must not count towards
        the station's coverage.
        """
        return not any(hop.endswith("*") for hop in self.path)


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
    # Bit 7 of an address's last byte is set once that digipeater has
    # repeated the packet.
    path = tuple(_callsign(hop) + ("*" if hop[6] & 0x80 else "") for hop in addresses[2:])
    return Packet(_callsign(addresses[1]), _callsign(addresses[0]), frame[at + 2 :], path)


def _degrees(degrees: bytes, minutes: bytes, hemisphere: bytes) -> tuple[float, int] | None:
    """Degrees, and how many digits the sender blanked.

    A blurred position is reported at the **middle** of what the sender
    was willing to say, not at the corner that reading blanks as zeros
    would give.
    """
    text = minutes.decode("latin-1")
    digits = text.replace(".", "")
    blanks = digits.count(" ")
    if blanks and digits.rstrip() != digits.rstrip(" ").rstrip():
        return None  # blanks must run from the right
    if blanks and digits.strip(" ").count(" "):
        return None
    try:
        filled = digits.replace(" ", "0")
        value = int(degrees) + (int(filled[:2]) + int(filled[2:]) / 100) / 60
    except ValueError:
        return None
    if blanks:
        value += BLUR_MINUTES[min(blanks, 4)] / 2 / 60
    return (-value if hemisphere in (b"S", b"W") else value), blanks


def blur_metres(blanks: int) -> float | None:
    """How far a blurred position could be out, in metres."""
    if not blanks:
        return None
    return round(BLUR_MINUTES[min(blanks, 4)] * METRES_PER_MINUTE)


def _base91(text: bytes) -> int:
    value = 0
    for byte in text:
        if not 33 <= byte <= 123:
            raise ValueError("not base 91")
        value = value * 91 + (byte - 33)
    return value


def _extension(rest: bytes) -> tuple[float | None, float | None, bytes]:
    """Course and speed, and what is left for the comment.

    `090/036` is a course and speed. `PHG5132` and its kin describe the
    station's own radio, not its movement. Both are seven characters.
    """
    match = COURSE_SPEED.match(rest)
    if match is not None:
        course = int(match.group(1))
        return (course % 360 if course else None), float(match.group(2)), rest[7:]
    if STATION_EXTENSION.match(rest) is not None:
        return None, None, rest[7:]
    return None, None, rest


def parse_weather(rest: bytes) -> tuple[dict[str, Any], bytes]:
    """Readings from a weather station, and what is left for the comment.

    With the `_` symbol the field that looks like a course and speed is
    the wind: direction in degrees, then speed in miles an hour. A
    weather station does not move, so neither becomes track or ground
    speed.

    A reading the station cannot make is sent as dots. It is consumed
    and left out, never stored as a zero, which would read as a real
    measurement of nothing.
    """
    weather: dict[str, Any] = {}
    wind = WIND_OR_MISSING.match(rest)
    if wind is not None:
        if wind.group(1).strip(b". ").isdigit():
            weather["wind_dir_deg"] = int(wind.group(1)) % 360
        if wind.group(2).strip(b". ").isdigit():
            weather["wind_kn"] = round(int(wind.group(2)) * 0.868976, 1)
        rest = rest[7:]
    # Stations write the readings in any order, so keep taking whichever
    # matches next until none does.
    taking = True
    while taking:
        taking = False
        for pattern, name, scale, offset in WEATHER:
            found = pattern.match(rest)
            if found is None:
                continue
            rest = rest[found.end() :]
            taking = True
            digits = found.group(1).strip(b". ")
            if digits.lstrip(b"-").isdigit():
                value = int(digits) + offset
                # A barometer reading zero is a station with no barometer.
                if not (name == "pressure_hpa" and value == 0):
                    weather[name] = round(value * scale, 2) if scale != 1.0 else value
            break
    return weather, rest


def wx_type(weather: dict[str, Any], rest: bytes) -> tuple[str | None, bytes]:
    """The short code naming the software and station, when that is all that is left."""
    text = rest.strip().decode("latin-1", "replace")
    if weather and text and len(text) <= MAX_WX_TYPE and " " not in text:
        return text, b""
    return None, rest


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
        north = _degrees(match.group(1), match.group(2), match.group(3))
        east = _degrees(match.group(5), match.group(6), match.group(7))
        if north is None or east is None:
            return None
        (lat, blanks), (lon, _) = north, east
        symbol = chr(match.group(4)[0]) + chr(match.group(8)[0])
        rest = body[match.end() :]
        weather: dict[str, Any] = {}
        course = speed = None
        if symbol[1] == WEATHER_SYMBOL:
            weather, rest = parse_weather(rest)
            found_type, rest = wx_type(weather, rest)
            if found_type:
                weather["wx_type"] = found_type
        else:
            course, speed, rest = _extension(rest)
        comment, extras, dao = read_tail(rest)
        weather.update(extras)
        lat, lon = apply_dao(lat, lon, dao)
        return {
            "lat": lat,
            "lon": lon,
            "course": course,
            "speed_kn": speed,
            "alt_m": _altitude_m(info),
            "symbol": symbol,
            "weather": weather,
            "comment": comment,
            "precision_m": blur_metres(blanks),
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
        symbol = chr(body[0]) + chr(body[9])
        course = speed = None
        c, s_byte = body[10], body[11]
        if 33 <= c <= 122 and c != 32 and chr(c) != "{":
            course = ((c - 33) * 4) % 360
            speed = 1.08 ** (s_byte - 33) - 1
        rest = body[13:]
        weather = {}
        if symbol[1] == WEATHER_SYMBOL:
            weather, rest = parse_weather(rest)
            found_type, rest = wx_type(weather, rest)
            if found_type:
                weather["wx_type"] = found_type
            course = speed = None
        comment, extras, dao = read_tail(rest)
        weather.update(extras)
        lat, lon = apply_dao(lat, lon, dao)
        return {
            "lat": lat,
            "lon": lon,
            "course": course,
            "speed_kn": speed,
            "alt_m": _altitude_m(info),
            "symbol": symbol,
            "weather": weather,
            "comment": comment,
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
    tail = info[9:]
    comment, extras, dao = read_tail(tail, mic_e=True)
    lat, lon = apply_dao(lat, lon, dao)
    return {
        "lat": lat,
        "lon": lon,
        "course": float(course % 360) if course else None,
        "speed_kn": float(speed),
        "alt_m": _mic_e_altitude(tail),
        "symbol": chr(body[7]) + chr(body[6]) if len(body) > 7 else None,
        "weather": extras,
        "comment": comment,
        "message": _mic_e_message(call),
    }


# What a radio appends to say which radio it is. APRS pairs a prefix
# before the position with a suffix after the comment, so a suffix is
# taken only when its prefix was there, and a comment ending in "=" is
# not mistaken for a Kenwood. Yaesu is the exception: `_` and one
# character, always at the end.
#
# These are the ones heard here. The community's full aprs-deviceid
# table is not vendored: it is CC BY-SA, and this repository has not
# chosen a licence yet.
PAIRED_DEVICES = {
    b"'": ((b"|3", "Byonics TinyTrak3"), (b"|4", "Byonics TinyTrak4")),
    b"]": ((b"=", "Kenwood TM-D710"),),
    b">": ((b"^", "Kenwood TH-D74"), (b"&", "Kenwood TH-D75"), (b"=", "Kenwood TH-D72")),
}
YAESU = {
    b"_ ": "Yaesu VX-8",
    b'_"': "Yaesu FTM-350",
    b"_#": "Yaesu VX-8G",
    b"_$": "Yaesu FT1D",
    b"_%": "Yaesu FTM-400DR",
    b"_(": "Yaesu FT2D",
    b"_)": "Yaesu FTM-100D",
}
DEVICE_PREFIXES = (b"`", b"'", b">", b"]")


def strip_device(tail: bytes, prefix: bytes = b"") -> tuple[bytes, str | None]:
    """Remove the radio's own code from around the comment, and name it."""
    if not prefix and tail[:1] in DEVICE_PREFIXES:
        prefix, tail = tail[:1], tail[1:]
    if len(tail) >= 2 and tail[-2:-1] == b"_":
        return tail[:-2].rstrip(), YAESU.get(tail[-2:])
    for suffix, name in PAIRED_DEVICES.get(prefix, ()):
        if tail.endswith(suffix):
            return tail[: -len(suffix)], name
    # A pipe is not something a person types, so these are taken even
    # when the prefix was read off earlier.
    for suffix, name in PAIRED_DEVICES[b"'"]:
        if tail.endswith(suffix):
            return tail[: -len(suffix)], name
    return tail, None


def take_telemetry(text: bytes) -> tuple[bytes, dict[str, Any]]:
    """Take base 91 telemetry from between two pipes.

    Two characters per value: a sequence number, then up to five
    channels. It is the radio talking about itself, not text.
    """
    found = TELEMETRY.search(text)
    if found is None or len(found.group(1)) % 2:
        return text, {}
    raw = found.group(1)
    values = [(raw[n] - 33) * 91 + (raw[n + 1] - 33) for n in range(0, len(raw), 2)]
    telemetry: dict[str, Any] = {"telemetry_seq": values[0]}
    for n, value in enumerate(values[1:], start=1):
        telemetry[f"telemetry_{n}"] = value
    return text[: found.start()] + text[found.end() :], telemetry


def take_dao(text: bytes) -> tuple[bytes, tuple[float, float] | None]:
    """Take the DAO extension, and the precision it adds, in degrees.

    `!W32!` spells one more digit of each coordinate's minutes, `!w32!`
    spells it in base 91. Without this the extra digit the sender took
    the trouble to send is thrown away.
    """
    found = DAO.search(text)
    if found is None:
        return text, None
    datum, lat_char, lon_char = found.group(1), found.group(2), found.group(3)
    cleaned = text[: found.start()] + text[found.end() :]
    if datum == b"W" and lat_char.isdigit() and lon_char.isdigit():
        return cleaned, (int(lat_char) / 1000 / 60, int(lon_char) / 1000 / 60)
    if datum == b"w" and 33 <= lat_char[0] <= 123 and 33 <= lon_char[0] <= 123:
        return cleaned, (
            (lat_char[0] - 33) / 91 * 0.01 / 60,
            (lon_char[0] - 33) / 91 * 0.01 / 60,
        )
    return cleaned, None


def apply_dao(lat: float, lon: float, dao: tuple[float, float] | None) -> tuple[float, float]:
    """Add the extra precision a sender sent, away from the equator and meridian."""
    if dao is None:
        return lat, lon
    return (
        lat + (dao[0] if lat >= 0 else -dao[0]),
        lon + (dao[1] if lon >= 0 else -dao[1]),
    )


def read_tail(tail: bytes, mic_e: bool = False) -> tuple[str | None, dict[str, Any], Any]:
    """What follows a position: the comment, what the radio added, and DAO.

    Order matters, because each piece sits inside the one before it: the
    radio's prefix, its altitude, telemetry, DAO, the text a person
    typed, and the radio's suffix.
    """
    prefix = tail[:1] if mic_e and tail[:1] in DEVICE_PREFIXES else b""
    rest = strip_mic_e_altitude(tail) if mic_e else tail
    rest, telemetry = take_telemetry(rest)
    rest, dao = take_dao(rest)
    # A plain packet names its radio in the destination, not the comment.
    rest, device = strip_device(rest, prefix) if mic_e else (rest, None)
    extras: dict[str, Any] = dict(telemetry)
    if device:
        extras["device"] = device
    return _comment(rest), extras, dao


def _mic_e_message(call: str) -> str | None:
    """The message a Mic-E station is showing, from its first three digits.

    Each of the first three destination characters carries one message
    bit as well as a latitude digit. The bits name one of eight standard
    messages, or a custom one when the characters come from A to K.
    """
    bits = 0
    custom = False
    for char in call[:3]:
        if "A" <= char <= "K":
            custom, bit = True, 1
        elif "P" <= char <= "Z":
            bit = 1
        elif "0" <= char <= "9":
            bit = 0
        else:
            return None  # `L` and the rest mean the digit was not sent
        bits = bits * 2 + bit
    if custom:
        return f"Custom {7 - bits}" if bits != 7 else "Custom 0"
    return MIC_E_MESSAGES[bits]


def strip_mic_e_altitude(tail: bytes) -> bytes:
    """Remove the radio prefix and the base 91 altitude before the comment."""
    rest = tail[1:] if tail[:1] in DEVICE_PREFIXES else tail
    at = rest.find(b"}")
    return rest[at + 1 :] if 0 <= at <= 4 else rest


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


# Path entries that name no station: a packet whose last used hop is one
# of these was repeated by somebody we cannot name.
ALIASES = (
    "WIDE", "TRACE", "RELAY", "ECHO", "GATE", "TEMP", "SGATE", "IGATE",
    "NOGATE", "RFONLY", "TCPIP", "TCPXX", "QA",
)  # fmt: skip
# How long two copies of one packet are treated as the same one.
DEDUP_S = 30.0


def is_alias(hop: str) -> bool:
    """True when a path entry is an alias rather than a callsign."""
    return hop.upper().split("-")[0].rstrip("*").startswith(ALIASES)


def via_of(path: tuple[str, ...]) -> str | None:
    """The station that repeated this packet to us, if we can name it.

    The digipeaters are in order, so the last one with its used bit set
    is the one that transmitted what we decoded. An alias there names
    nobody, and an earlier callsign is not who we heard.
    """
    used = [hop for hop in path if hop.endswith("*")]
    if not used:
        return None
    last = used[-1].rstrip("*")
    return None if is_alias(last) else last.lower()


def parse_object(info: bytes) -> tuple[str, str, bytes] | None:
    """An object or item: its name, which of the two it is, and the rest.

    A killed object or item returns None: it is a station saying the
    thing is gone, not saying where it is.
    """
    kind = chr(info[0])
    if kind == OBJECT:
        if len(info) < 18:
            return None
        name, flag, rest = info[1:10], chr(info[10]), info[11:]
        if flag != "*":
            return None  # killed
        return _clean_name(name), "object", rest[7:]  # a timestamp, then the position
    at = -1
    for n, byte in enumerate(info[1:11], start=1):
        if byte in (0x21, 0x5F):  # `!` live, `_` killed
            at = n
            break
    if at < 4 or chr(info[at]) == "_":
        return None
    return _clean_name(info[1:at]), "item", info[at + 1 :]


def _clean_name(raw: bytes) -> str:
    name = raw.decode("latin-1", "replace").strip().lower()
    return NAME_UNSAFE.sub("_", name).strip("_")[:9]


def parse_third_party(info: bytes) -> tuple[str, bytes] | None:
    """A packet that reached us from somewhere else: its source and payload.

    `}` carries another station's packet, forwarded by a gateway. What we
    heard is the gateway, never the station inside.
    """
    at = info.find(b":")
    if at < 0:
        return None
    header = info[1:at].decode("latin-1", "replace")
    source, _, _ = header.partition(">")
    if not source:
        return None
    return source.strip().lower(), info[at + 1 :]


class Direwolf:
    """Turn direwolf's KISS stream into APRS observations."""

    def __init__(self, kind: str = "aprs") -> None:
        self.kind = kind
        self.frames = KissFramer()
        self.bad = 0
        self.no_position = 0
        self.repeats = 0
        self._seen: dict[tuple[str, bytes], tuple[float, bool]] = {}

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
        if len(self._seen) > 4000:
            self._forget(now)
        return out

    def _forget(self, now: float) -> None:
        self._seen = {key: seen for key, seen in self._seen.items() if now - seen[0] <= DEDUP_S}

    def _repeat(self, source: str, info: bytes, direct: bool, now: float) -> bool:
        """True when this is another copy of a packet we already sent.

        A packet travels several paths, so the same beacon arrives more
        than once. A later copy that came to us directly is still sent,
        because only a direct copy says what this receiver can hear.
        """
        key = (source, info)
        seen = self._seen.get(key)
        if seen is not None and now - seen[0] <= DEDUP_S and not (direct and not seen[1]):
            self.repeats += 1
            return True
        self._seen[key] = (now, direct or (seen[1] if seen else False))
        return False

    def frame(self, frame: bytes, now: float) -> Observation | None:
        packet = parse_ax25(frame)
        if packet is None:
            self.bad += 1
            return None

        source, info = packet.source, packet.info
        # The frame as heard, before a third party or an object rewrites
        # what `source` and `info` mean below.
        raw = tnc2(packet)
        direct, via = packet.direct, via_of(packet.path)
        third_party = False
        if info[:1].decode("latin-1") == THIRD_PARTY:
            inner = parse_third_party(info)
            if inner is None:
                self.bad += 1
                return None
            # Whatever the inner path says, what we heard is the gateway.
            source, info = inner
            third_party, direct = True, False
            via = via or packet.source
        if not info:
            self.no_position += 1
            return None

        owner = object_kind = None
        if info[:1].decode("latin-1") in (OBJECT, ITEM):
            placed = parse_object(info)
            if placed is None:
                self.no_position += 1
                return None
            name, object_kind, rest = placed
            # Every identity we send is lower case, as Sonoran stores them.
            owner, source = source.lower(), ";" + name
            info = b"!" + rest  # the rest reads as a position report
            via = via or (owner if not direct else None)

        if info[:1].decode("latin-1") in MIC_E:
            found = parse_mic_e(packet.destination, info)
        else:
            found = parse_position(info)
        if found is None:
            self.no_position += 1
            return None
        if self._repeat(source, packet.info, direct, now):
            return None

        speed = found.get("speed_kn")
        point = observation(
            self.kind,
            source,
            now,
            found["lat"],
            found["lon"],
            alt_m=found.get("alt_m"),
            alt_ref="geom" if found.get("alt_m") is not None else None,
            gs_kn=speed if speed else None,
            track_deg=found.get("course"),
            direct=direct,
            via=None if direct else via,
            precision_m=found.get("precision_m"),
            raw=raw,
            meta={
                "symbol": found.get("symbol"),
                # Mic-E's destination is encoded latitude, which says
                # nothing to a reader; it carries the message instead.
                "destination": None if "message" in found else packet.destination,
                "message": found.get("message"),
                "comment": found.get("comment"),
                "path": ",".join(packet.path) or None,
                "owner": owner,
                "object": object_kind,
                "third_party": True if third_party else None,
                **found.get("weather", {}),
            },
        )
        if point is None:
            self.bad += 1
        return point


def tnc2(packet: Packet) -> str:
    """The frame as a TNC2 monitor line, the form every APRS tool prints.

    `SRC>DEST,PATH:info`, with `*` on each hop that has repeated it. A
    byte that is not printable is written `<0xNN>`, so the line stays
    readable and carries no control characters.
    """
    path = "".join("," + hop for hop in packet.path)
    return f"{packet.source}>{packet.destination}{path}:{printable(packet.info)}"


def printable(info: bytes) -> str:
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else f"<0x{byte:02x}>" for byte in info)


def _comment(rest: bytes) -> str | None:
    """The free text a station types, whole.

    Only what is left after the position and its extensions, and never
    shortened: an AX.25 information field holds at most 256 characters,
    so a comment always fits what Sonoran stores.
    """
    text = ALTITUDE.sub(b"", rest).decode("latin-1", "replace")
    text = "".join(char for char in text if char.isprintable()).strip()
    return text or None
