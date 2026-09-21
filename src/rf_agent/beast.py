"""Read readsb's Beast output: frames in, aircraft positions out.

Beast is binary. Each frame is `0x1a`, a type byte, a 6 byte timestamp,
a signal byte and the Mode S message. A `0x1a` inside a frame is sent
twice. Type `1` is Mode A/C (2 bytes), `2` a short Mode S frame (7) and
`3` a long one (14). Anything else is skipped.

`BeastFramer` cuts a byte stream into frames. It keeps partial frames
across reads and finds the next frame after garbage, so a decoder that
restarts mid-frame costs one frame, never the connection.

`Tracker` keeps a little state per aircraft, because an ADS-B position
arrives in halves: CPR needs an even and an odd frame, and the callsign
and speed come in other messages.

The 12 MHz timestamp in each frame is a counter, not a clock, so the
time of a point is when the agent read it.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

from rf_agent.geo import distance_m
from rf_agent.messages import Observation, observation
from rf_agent.modes import (
    Message,
    address_of,
    cpr_global,
    cpr_local,
    decode_es,
    overlaid_address,
    squawk,
)

ESC = 0x1A
# Type byte to message length.
LENGTHS = {0x31: 2, 0x32: 7, 0x33: 14}
HEADER = 7  # timestamp (6) and signal (1)
MAX_BUFFER = 1 << 16

# How long two CPR halves may be apart and still pair (DO-260B: 10 s).
PAIR_S = 10.0
# How long a decoded position may serve as the reference for the next
# one. An airliner moves about 5 km in 20 s, well inside what a local
# decode allows.
REFERENCE_S = 20.0
# A speed or callsign older than this is left off a point.
STALE_S = 60.0
# An aircraft not heard for this long is forgotten.
FORGET_S = 300.0
# Nothing a station hears is further away than this. A position beyond
# it is a bad decode.
MAX_RANGE_M = 600_000.0
# Faster than anything a 1090 MHz receiver should hear, in metres a second.
MAX_SPEED_MS = 1000.0


@dataclass(frozen=True, slots=True)
class Frame:
    kind: int
    # The receiver's 12 MHz counter. Not a clock; used to replay a capture.
    ticks: int
    signal: int
    message: bytes


class BeastFramer:
    """Cut a Beast byte stream into frames. Counts what it had to skip."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.skipped = 0

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buf += data
        buf = self._buf
        pos = 0
        while True:
            start = buf.find(ESC, pos)
            if start < 0:
                self.skipped += int(len(buf) > pos)
                pos = len(buf)
                break
            if start > pos:
                self.skipped += 1
            if start + 1 >= len(buf):
                pos = start
                break
            kind = buf[start + 1]
            length = LENGTHS.get(kind)
            if length is None:
                # An unknown type, or the second half of an escaped 0x1a
                # read out of step. Resynchronise on the next 0x1a.
                self.skipped += int(kind != ESC)
                pos = start + 1 + int(kind == ESC)
                continue
            body, end = self._unescape(buf, start + 2, HEADER + length)
            if body is None:
                if end < 0:
                    pos = start  # incomplete; wait for more
                    break
                self.skipped += 1  # a new frame began inside this one
                pos = end
                continue
            pos = end
            yield Frame(kind, int.from_bytes(body[:6]), body[6], bytes(body[HEADER:]))
        del buf[:pos]
        if len(buf) > MAX_BUFFER:
            self.skipped += 1
            buf.clear()

    @staticmethod
    def _unescape(buf: bytearray, at: int, want: int) -> tuple[bytearray | None, int]:
        """Read `want` unescaped bytes from `at`.

        Returns (bytes, end) when complete, (None, -1) when the buffer
        ends first, and (None, where) when a lone 0x1a starts a new frame
        at `where`.
        """
        out = bytearray()
        i = at
        n = len(buf)
        while len(out) < want:
            if i >= n:
                return None, -1
            byte = buf[i]
            if byte == ESC:
                if i + 1 >= n:
                    return None, -1
                if buf[i + 1] != ESC:
                    return None, i
                i += 1
            out.append(byte)
            i += 1
        return out, i


def encode_frame(frame: Frame) -> bytes:
    """Write a frame as Beast, the inverse of `BeastFramer`.

    The agent never sends Beast. `tools/shift_capture.py` and the tests
    use this to write captures.
    """
    body = frame.ticks.to_bytes(6) + bytes([frame.signal]) + frame.message
    return bytes([ESC, frame.kind]) + body.replace(b"\x1a", b"\x1a\x1a")


def rssi_db(signal: int) -> float | None:
    """The Beast signal byte as dB relative to full scale, as readsb shows it."""
    if signal <= 0:
        return None
    return round(20 * math.log10(signal / 255), 1)


@dataclass
class _Aircraft:
    seen: float
    even: tuple[tuple[int, int], float] | None = None
    odd: tuple[tuple[int, int], float] | None = None
    position: tuple[float, float, float] | None = None  # lat, lon, time
    alt: tuple[float, str, float] | None = None
    velocity: tuple[float, float | None, float] | None = None  # gs, track, time
    callsign: tuple[str, float] | None = None
    category: tuple[str, float] | None = None
    squawk: tuple[str, float] | None = None
    on_ground: bool = False


class Tracker:
    """Turn Beast frames into observations, one per decoded position."""

    def __init__(self, kind: str = "adsb", station: tuple[float, float] | None = None) -> None:
        self.kind = kind
        # The true antenna position. Used here, never sent.
        self.station = station
        self.aircraft: dict[str, _Aircraft] = {}
        self.framer = BeastFramer()
        self.bad = 0
        self._last_forget = 0.0

    def feed(self, data: bytes, now: float) -> list[Observation]:
        """Handle bytes read from the decoder at time `now`."""
        out: list[Observation] = []
        for frame in self.framer.feed(data):
            try:
                point = self.frame(frame, now)
            except Exception:  # a frame must never stop the reader
                self.bad += 1
                continue
            if point is not None:
                out.append(point)
        if now - self._last_forget > 10:
            self.forget(now)
        return out

    def forget(self, now: float) -> None:
        self._last_forget = now
        for address in [a for a, s in self.aircraft.items() if now - s.seen > FORGET_S]:
            del self.aircraft[address]

    def frame(self, frame: Frame, now: float) -> Observation | None:
        msg = frame.message
        if frame.kind != 0x33 and frame.kind != 0x32:
            return None
        df = msg[0] >> 3
        if df in (5, 21):
            self._identity_reply(msg, now)
            return None
        if df not in (17, 18) or len(msg) != 14 or address_of(msg) is None:
            return None
        es = decode_es(msg)
        if es is None:
            self.bad += 1
            return None
        ac = self.aircraft.get(es.address)
        if ac is None:
            ac = self.aircraft[es.address] = _Aircraft(seen=now)
        ac.seen = now
        if es.callsign:
            ac.callsign = (es.callsign, now)
        if es.category:
            ac.category = (es.category, now)
        if es.squawk:
            ac.squawk = (es.squawk, now)
        if es.gs_kn is not None and es.cpr is None:
            ac.velocity = (es.gs_kn, es.track_deg, now)
        if es.cpr is None:
            return None
        return self._position(es.address, ac, es, frame.signal, now)

    def _identity_reply(self, msg: bytes, now: float) -> None:
        """Take the squawk from a DF5 or DF21 reply of an aircraft already known.

        These replies carry no parity of their own: it is overlaid with
        the address. A reply whose address is not one we track is noise.
        """
        address = f"{overlaid_address(msg):06x}"
        ac = self.aircraft.get(address)
        if ac is None or now - ac.seen > STALE_S:
            return
        ac.squawk = (squawk(int.from_bytes(msg[0:4]) & 0x1FFF), now)

    def _position(
        self, address: str, ac: _Aircraft, es: Message, signal: int, now: float
    ) -> Observation | None:
        if es.surface:
            ac.on_ground = True
            if es.gs_kn is not None:
                ac.velocity = (es.gs_kn, es.track_deg, now)
            if self.station is None:
                return None  # a surface position needs a nearby reference
            lat, lon = cpr_local(es.cpr, es.odd, *self.station, surface=True)
        else:
            ac.on_ground = False
            if es.alt_m is not None:
                ac.alt = (es.alt_m, es.alt_ref, now)
            if es.odd:
                ac.odd = (es.cpr, now)
            else:
                ac.even = (es.cpr, now)
            decoded = self._airborne(ac, es, now)
            if decoded is None:
                return None
            lat, lon = decoded

        if self.station is not None and distance_m(*self.station, lat, lon) > MAX_RANGE_M:
            self.bad += 1
            return None
        if ac.position is not None:
            last_lat, last_lon, last_t = ac.position
            elapsed = max(now - last_t, 1.0)
            if distance_m(last_lat, last_lon, lat, lon) > MAX_SPEED_MS * elapsed + 2000:
                # A jump no aircraft makes. Drop the reference and the
                # halves, and start again from a fresh pair.
                ac.position = None
                ac.even = ac.odd = None
                self.bad += 1
                return None
        ac.position = (lat, lon, now)
        return self._observation(address, ac, lat, lon, signal, now)

    def _airborne(self, ac: _Aircraft, es: Message, now: float) -> tuple[float, float] | None:
        if ac.position is not None and now - ac.position[2] <= REFERENCE_S:
            return cpr_local(es.cpr, es.odd, ac.position[0], ac.position[1])
        if ac.even is None or ac.odd is None:
            return None
        if abs(ac.even[1] - ac.odd[1]) > PAIR_S:
            return None
        return cpr_global(ac.even[0], ac.odd[0], newest_odd=es.odd)

    def _observation(
        self, address: str, ac: _Aircraft, lat: float, lon: float, signal: int, now: float
    ) -> Observation | None:
        def fresh(item):
            return item is not None and now - item[-1] <= STALE_S

        gs = track = None
        if fresh(ac.velocity):
            gs, track = ac.velocity[0], ac.velocity[1]
        alt = ref = None
        if not ac.on_ground and fresh(ac.alt):
            alt, ref = ac.alt[0], ac.alt[1]
        meta = {
            "callsign": ac.callsign[0] if fresh(ac.callsign) else None,
            "squawk": ac.squawk[0] if fresh(ac.squawk) else None,
            "category": ac.category[0] if fresh(ac.category) else None,
            "ground": True if ac.on_ground else None,
        }
        return observation(
            self.kind,
            address,
            now,
            lat,
            lon,
            alt_m=alt,
            alt_ref=ref,
            gs_kn=gs,
            track_deg=track,
            rssi_db=rssi_db(signal),
            meta=meta,
        )
