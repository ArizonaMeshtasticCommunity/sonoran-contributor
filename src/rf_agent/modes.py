"""Mode S and ADS-B decoding, as much as a map needs.

The Beast reader hands over raw Mode S frames. This module checks their
parity and decodes the extended squitter (DF17, and DF18 with an aircraft
address): identity, position, velocity. Positions use CPR, the compact
position format: a pair of even and odd frames gives a position anywhere
("global"), and one frame gives a position near a known one ("local").

The rules are those of DO-260B, as written up in Junzi Sun, "The 1090
Megahertz Riddle". Every function is pure, so the tests can feed it the
book's example frames.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

POLY = 0xFFF409
CPR_SCALE = 1 << 17
NZ = 15

_CRC_TABLE: list[int] = []
for _byte in range(256):
    _crc = _byte << 16
    for _ in range(8):
        _crc = ((_crc << 1) ^ POLY) if _crc & 0x800000 else (_crc << 1)
    _CRC_TABLE.append(_crc & 0xFFFFFF)

CHARSET = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"


def crc(data: bytes) -> int:
    """Return the Mode S parity of `data`, the frame without its last 3 bytes."""
    value = 0
    for byte in data:
        value = ((value << 8) & 0xFFFFFF) ^ _CRC_TABLE[((value >> 16) ^ byte) & 0xFF]
    return value


def parity_ok(frame: bytes) -> bool:
    """True when an extended squitter's parity field matches its content."""
    return crc(frame[:-3]) == int.from_bytes(frame[-3:])


def overlaid_address(frame: bytes) -> int:
    """Return the address a DF4, 5, 20 or 21 reply's parity is overlaid with."""
    return crc(frame[:-3]) ^ int.from_bytes(frame[-3:])


def _bits(me: int, first: int, last: int) -> int:
    """Bits `first` to `last` of the 56-bit ME field, counted from 1."""
    return (me >> (56 - last)) & ((1 << (last - first + 1)) - 1)


def nl(lat: float) -> int:
    """The number of longitude zones at a latitude."""
    lat = abs(lat)
    if lat == 0:
        return 59
    if lat == 87:
        return 2
    if lat > 87:
        return 1
    a = 1 - math.cos(math.pi / (2 * NZ))
    b = math.cos(math.radians(lat)) ** 2
    return math.floor(2 * math.pi / math.acos(1 - a / b))


def cpr_global(
    even: tuple[int, int], odd: tuple[int, int], newest_odd: bool
) -> tuple[float, float] | None:
    """Airborne position from an even and an odd frame, each (lat, lon) in CPR units.

    Returns None when the two frames lie in different longitude zones,
    which means they were not close enough in time to pair.
    """
    lat_e, lon_e = even[0] / CPR_SCALE, even[1] / CPR_SCALE
    lat_o, lon_o = odd[0] / CPR_SCALE, odd[1] / CPR_SCALE
    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    rlat_e = (360 / 60) * (j % 60 + lat_e)
    rlat_o = (360 / 59) * (j % 59 + lat_o)
    if rlat_e >= 270:
        rlat_e -= 360
    if rlat_o >= 270:
        rlat_o -= 360
    if not (-90 <= rlat_e <= 90 and -90 <= rlat_o <= 90):
        return None
    if nl(rlat_e) != nl(rlat_o):
        return None
    lat = rlat_o if newest_odd else rlat_e
    zones = nl(lat)
    ni = max(zones - (1 if newest_odd else 0), 1)
    m = math.floor(lon_e * (zones - 1) - lon_o * zones + 0.5)
    lon = (360 / ni) * (m % ni + (lon_o if newest_odd else lon_e))
    if lon >= 180:
        lon -= 360
    return lat, lon


def cpr_local(
    cpr: tuple[int, int], odd: bool, ref_lat: float, ref_lon: float, surface: bool = False
) -> tuple[float, float]:
    """Position from one frame, near a reference position.

    Correct when the reference is within about 300 km of the aircraft
    (45 km for a surface position). The caller must know that it is.
    """
    span = 90.0 if surface else 360.0
    i = 1 if odd else 0
    lat_c, lon_c = cpr[0] / CPR_SCALE, cpr[1] / CPR_SCALE
    dlat = span / (60 - i)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + (ref_lat % dlat) / dlat - lat_c)
    lat = dlat * (j + lat_c)
    zones = nl(lat) - i
    dlon = span / zones if zones > 0 else span
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + (ref_lon % dlon) / dlon - lon_c)
    lon = dlon * (m + lon_c)
    if lon >= 180:
        lon -= 360
    elif lon < -180:
        lon += 360
    return lat, lon


def cpr_encode(lat: float, lon: float, odd: bool, surface: bool = False) -> tuple[int, int]:
    """Encode a position as CPR, the inverse of the two decoders.

    The agent never transmits. `tools/shift_capture.py` uses this to move
    a recorded capture somewhere else, and the tests use it to build
    frames.
    """
    span = 90.0 if surface else 360.0
    i = 1 if odd else 0
    dlat = span / (60 - i)
    yz = math.floor(CPR_SCALE * (lat % dlat) / dlat + 0.5)
    rlat = dlat * (yz / CPR_SCALE + math.floor(lat / dlat))
    zones = nl(rlat) - i
    dlon = span / zones if zones > 0 else span
    xz = math.floor(CPR_SCALE * (lon % dlon) / dlon + 0.5)
    return yz & 0x1FFFF, xz & 0x1FFFF


def altitude_ft(code: int) -> int | None:
    """Decode the 12-bit altitude of an airborne position, in feet.

    Only the 25 ft form (Q bit set) is decoded. The older 100 ft Gillham
    form is rare in ADS-B and is left as unknown.
    """
    if code == 0 or not code & 0x10:
        return None
    n = ((code & 0xFE0) >> 1) | (code & 0x0F)
    return n * 25 - 1000


def squawk(id13: int) -> str:
    """Decode a 13-bit identity field (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4)."""

    def bit(n: int) -> int:
        return (id13 >> (13 - n)) & 1

    a = bit(6) * 4 + bit(4) * 2 + bit(2)
    b = bit(12) * 4 + bit(10) * 2 + bit(8)
    c = bit(5) * 4 + bit(3) * 2 + bit(1)
    d = bit(13) * 4 + bit(11) * 2 + bit(9)
    return f"{a}{b}{c}{d}"


# Surface movement code to ground speed in knots: (first code, speed at
# it, knots per step). DO-260B table.
_MOVEMENT = (
    (2, 0.125, 0.125),
    (9, 1.0, 0.25),
    (13, 2.0, 0.5),
    (39, 15.0, 1.0),
    (94, 70.0, 2.0),
    (109, 100.0, 5.0),
)


def movement_kn(code: int) -> float | None:
    """Ground speed from a surface position's movement code."""
    if code == 1:
        return 0.0
    if code == 124:
        return 175.0
    for first, speed, step in reversed(_MOVEMENT):
        if first <= code < 124:
            return speed + (code - first) * step
    return None


@dataclass(frozen=True, slots=True)
class Message:
    """What one extended squitter says. Unset fields are None."""

    address: str
    tc: int
    callsign: str | None = None
    category: str | None = None
    cpr: tuple[int, int] | None = None
    odd: bool = False
    surface: bool = False
    alt_m: float | None = None
    alt_ref: str | None = None
    gs_kn: float | None = None
    track_deg: float | None = None
    squawk: str | None = None


def address_of(frame: bytes) -> str | None:
    """The aircraft address of a DF17 or DF18 frame, as readsb prints it.

    DF18 carries ADS-B from things that are not transponders. Control
    field 0 has an ICAO address; 1 has another kind, which readsb marks
    with a `~`. TIS-B and ADS-R rebroadcasts (2, 3, 5, 6) are left out.
    """
    df = frame[0] >> 3
    hexa = frame[1:4].hex()
    if df == 17:
        return hexa
    if df == 18:
        cf = frame[0] & 7
        if cf == 0:
            return hexa
        if cf == 1:
            return "~" + hexa
    return None


def decode_es(frame: bytes) -> Message | None:
    """Decode a 112-bit DF17 or DF18 frame with good parity, or return None."""
    if len(frame) != 14 or not parity_ok(frame):
        return None
    address = address_of(frame)
    if address is None:
        return None
    me = int.from_bytes(frame[4:11])
    tc = _bits(me, 1, 5)

    if 1 <= tc <= 4:
        chars = "".join(CHARSET[_bits(me, 9 + 6 * n, 14 + 6 * n)] for n in range(8))
        callsign = chars.replace("#", "").strip() or None
        ca = _bits(me, 6, 8)
        category = f"{'DCBA'[tc - 1]}{ca}" if ca else None
        return Message(address, tc, callsign=callsign, category=category)

    if 5 <= tc <= 8:
        mov = movement_kn(_bits(me, 6, 12))
        track = _bits(me, 14, 20) * 360 / 128 if _bits(me, 13, 13) else None
        return Message(
            address,
            tc,
            cpr=(_bits(me, 23, 39), _bits(me, 40, 56)),
            odd=bool(_bits(me, 22, 22)),
            surface=True,
            gs_kn=mov,
            track_deg=track,
        )

    if 9 <= tc <= 18 or 20 <= tc <= 22:
        code = _bits(me, 9, 20)
        alt_m: float | None
        if tc <= 18:
            feet = altitude_ft(code)
            alt_m, ref = (feet * 0.3048 if feet is not None else None), "baro"
        else:
            # Height above the ellipsoid from GNSS, in metres.
            alt_m, ref = (float(code) if code else None), "geom"
        return Message(
            address,
            tc,
            cpr=(_bits(me, 23, 39), _bits(me, 40, 56)),
            odd=bool(_bits(me, 22, 22)),
            alt_m=alt_m,
            alt_ref=ref if alt_m is not None else None,
        )

    if tc == 19:
        subtype = _bits(me, 6, 8)
        if subtype not in (1, 2):
            # Airspeed and heading, not ground speed and track.
            return Message(address, tc)
        vew, vns = _bits(me, 15, 24), _bits(me, 26, 35)
        if not vew or not vns:
            return Message(address, tc)
        factor = 4 if subtype == 2 else 1
        vx = (vew - 1) * factor * (-1 if _bits(me, 14, 14) else 1)
        vy = (vns - 1) * factor * (-1 if _bits(me, 25, 25) else 1)
        track = math.degrees(math.atan2(vx, vy)) % 360
        return Message(address, tc, gs_kn=math.hypot(vx, vy), track_deg=track)

    if tc == 28 and _bits(me, 6, 8) == 1:
        return Message(address, tc, squawk=squawk(_bits(me, 12, 24)))

    return Message(address, tc)
