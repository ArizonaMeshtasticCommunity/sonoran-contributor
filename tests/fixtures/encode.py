"""Build Mode S frames, for the test captures.

The inverse of `rf_agent.modes`, for the few messages the captures need.
Every frame gets correct parity, so the decoder reads it as a real one.
"""

from __future__ import annotations

import math

from rf_agent.modes import CHARSET, cpr_encode, crc


def _es(df: int, low3: int, icao: str, me: int) -> bytes:
    head = bytes([(df << 3) | low3]) + bytes.fromhex(icao) + me.to_bytes(7)
    return head + crc(head).to_bytes(3)


def es(icao: str, me: int, df18_cf: int | None = None) -> bytes:
    """A DF17 frame, or a DF18 one with the given control field."""
    if df18_cf is None:
        return _es(17, 5, icao, me)
    return _es(18, df18_cf, icao, me)


def ident_me(callsign: str, tc: int = 4, ca: int = 3) -> int:
    me = (tc << 51) | (ca << 48)
    for n, char in enumerate(callsign.ljust(8)[:8]):
        me |= CHARSET.index(char) << (42 - 6 * n)
    return me


def alt_code(feet: float) -> int:
    n = round((feet + 1000) / 25)
    return ((n >> 4) << 5) | 0x10 | (n & 0xF)


def position_me(lat: float, lon: float, odd: bool, feet: float, tc: int = 11) -> int:
    ylat, xlon = cpr_encode(lat, lon, odd)
    return (tc << 51) | (alt_code(feet) << 36) | (int(odd) << 34) | (ylat << 17) | xlon


def gnss_position_me(lat: float, lon: float, odd: bool, metres: int) -> int:
    ylat, xlon = cpr_encode(lat, lon, odd)
    return (20 << 51) | (metres << 36) | (int(odd) << 34) | (ylat << 17) | xlon


def surface_me(lat: float, lon: float, odd: bool, movement: int, heading: float) -> int:
    ylat, xlon = cpr_encode(lat, lon, odd, surface=True)
    track = round(heading * 128 / 360) % 128
    return (
        (7 << 51)
        | (movement << 44)
        | (1 << 43)
        | (track << 36)
        | (int(odd) << 34)
        | (ylat << 17)
        | xlon
    )


def velocity_me(gs_kn: float, track_deg: float) -> int:
    vx = round(gs_kn * math.sin(math.radians(track_deg)))
    vy = round(gs_kn * math.cos(math.radians(track_deg)))
    return (
        (19 << 51)
        | (1 << 48)
        | (int(vx < 0) << 42)
        | ((abs(vx) + 1) << 32)
        | (int(vy < 0) << 31)
        | ((abs(vy) + 1) << 21)
        | (1 << 10)  # vertical rate 0
    )


def id13(code: str) -> int:
    """Encode a squawk as the 13-bit identity field."""
    a, b, c, d = (int(x) for x in code)
    bits = {
        1: c & 1, 2: a & 1, 3: (c >> 1) & 1, 4: (a >> 1) & 1, 5: (c >> 2) & 1,
        6: (a >> 2) & 1, 7: 0, 8: b & 1, 9: d & 1, 10: (b >> 1) & 1,
        11: (d >> 1) & 1, 12: (b >> 2) & 1, 13: (d >> 2) & 1,
    }  # fmt: skip
    return sum(bit << (13 - n) for n, bit in bits.items())


def identity_reply(icao: str, code: str) -> bytes:
    """A DF5 surveillance reply: parity overlaid with the address."""
    head = ((5 << 27) | id13(code)).to_bytes(4)
    return head + (crc(head) ^ int(icao, 16)).to_bytes(3)


# --- APRS: KISS, AX.25 and Mic-E -------------------------------------


def kiss_frame(payload: bytes, port: int = 0) -> bytes:
    """Wrap one AX.25 frame as KISS, escaping the delimiters."""
    body = bytes([port << 4]) + payload
    body = body.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
    return b"\xc0" + body + b"\xc0"


def _address(call: str, last: bool = False) -> bytes:
    name, _, ssid = call.upper().partition("-")
    out = bytes((ord(c) << 1) & 0xFE for c in name.ljust(6)[:6])
    return out + bytes([0x60 | ((int(ssid or 0) & 0x0F) << 1) | int(last)])


def ax25(source: str, destination: str, info: bytes, digis: tuple[str, ...] = ()) -> bytes:
    """A UI frame with no layer 3 protocol: what APRS travels in."""
    path = [_address(destination), _address(source)] + [_address(d) for d in digis]
    path[-1] = path[-1][:6] + bytes([path[-1][6] | 1])
    return b"".join(path) + bytes([0x03, 0xF0]) + info


def _split(value: float) -> tuple[int, int, int]:
    """Degrees, minutes and hundredths of a minute, carrying as needed.

    Rounding the hundredths on their own turns 47.999 minutes into "100"
    hundredths, which is a digit too many.
    """
    degrees = int(value)
    hundredths = round((value - degrees) * 6000)
    if hundredths >= 6000:
        degrees, hundredths = degrees + 1, 0
    return degrees, hundredths // 100, hundredths % 100


def mic_e(
    lat: float, lon: float, speed_kn: int, course: int, symbol: str = "/>"
) -> tuple[str, bytes]:
    """Encode a Mic-E packet. Returns (destination callsign, info field)."""
    north, west = lat >= 0, lon < 0
    lat, lon = abs(lat), abs(lon)
    lat_deg, lat_min, lat_hun = _split(lat)
    digits = f"{lat_deg:02d}{lat_min:02d}{lat_hun:02d}"
    offset = lon >= 100
    flags = (north, offset, west)
    call = "".join(
        chr(ord("P" if flags[n - 3] else "0") + int(d)) if n >= 3 else chr(ord("0") + int(d))
        for n, d in enumerate(digits)
    )

    lon_deg, minutes, hundredths = _split(lon)
    degrees = lon_deg - (100 if offset else 0)
    info = bytes(
        [
            ord("`"),
            degrees + 28,
            (minutes + 60 if minutes < 10 else minutes) + 28,
            hundredths + 28,
            speed_kn // 10 + 28,
            (speed_kn % 10) * 10 + course // 100 + 28,
            course % 100 + 28,
            ord(symbol[1]),
            ord(symbol[0]),
        ]
    )
    return call, info


def _base91(value: int, width: int) -> bytes:
    out = bytearray()
    for _ in range(width):
        out.append(33 + value % 91)
        value //= 91
    return bytes(reversed(out))


def aprs_compressed(lat: float, lon: float, symbol: str = "/>") -> bytes:
    """A compressed position report, base 91, with no course or speed."""
    y = _base91(round(380926 * (90 - lat)), 4)
    x = _base91(round(190463 * (180 + lon)), 4)
    return b"!" + symbol[0].encode() + y + x + symbol[1].encode() + b" sT"
