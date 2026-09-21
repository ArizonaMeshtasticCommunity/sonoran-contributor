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
