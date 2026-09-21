"""Mode S decoding, against the published example frames."""

import pytest

from rf_agent.modes import (
    _bits,
    altitude_ft,
    cpr_encode,
    cpr_global,
    cpr_local,
    crc,
    decode_es,
    movement_kn,
    nl,
    parity_ok,
    squawk,
)

from encode import id13

# From Junzi Sun, "The 1090 Megahertz Riddle".
IDENT = bytes.fromhex("8D4840D6202CC371C32CE0576098")
EVEN = bytes.fromhex("8D40621D58C382D690C8AC2863A7")
ODD = bytes.fromhex("8D40621D58C386435CC412692AD6")
VELOCITY = bytes.fromhex("8D485020994409940838175B284F")


def test_parity():
    assert parity_ok(bytes.fromhex("8D406B902015A678D4D220AA4BDA"))
    assert parity_ok(IDENT)
    broken = bytearray(IDENT)
    broken[5] ^= 1
    assert not parity_ok(bytes(broken))
    assert crc(b"") == 0


def test_identification():
    msg = decode_es(IDENT)
    assert msg.address == "4840d6"
    assert msg.callsign == "KLM1023"
    assert msg.category is None  # emitter category 0: no information


def test_airborne_position_global_and_local():
    even, odd = decode_es(EVEN), decode_es(ODD)
    assert not even.odd and odd.odd
    assert even.alt_m == pytest.approx(38000 * 0.3048)
    assert even.alt_ref == "baro"
    lat, lon = cpr_global(even.cpr, odd.cpr, newest_odd=False)
    assert lat == pytest.approx(52.25720, abs=1e-5)
    assert lon == pytest.approx(3.91937, abs=1e-5)
    lat, lon = cpr_local(even.cpr, False, 52.258, 3.918)
    assert (lat, lon) == pytest.approx((52.25720, 3.91937), abs=1e-5)


def test_velocity():
    msg = decode_es(VELOCITY)
    assert msg.gs_kn == pytest.approx(159.20, abs=0.01)
    assert msg.track_deg == pytest.approx(182.88, abs=0.01)


def test_surface_position_local():
    # pyModeS's surface example; its parity is zeroed, so decode the fields.
    frame = bytes.fromhex("8FC8200A3AB8F5F893096B000000")
    me = int.from_bytes(frame[4:11])
    cpr = (_bits(me, 23, 39), _bits(me, 40, 56))
    lat, lon = cpr_local(cpr, bool(_bits(me, 22, 22)), -43.5, 172.5, surface=True)
    assert (lat, lon) == pytest.approx((-43.48564, 172.53942), abs=1e-4)


@pytest.mark.parametrize(
    "lat,lon",
    [(32.8, -113.3), (33.42, -111.94), (-33.9, 151.2), (0.1, 0.1), (64.8, -147.7), (-0.2, 179.9)],
)
def test_cpr_round_trip(lat, lon):
    even, odd = cpr_encode(lat, lon, False), cpr_encode(lat, lon, True)
    for newest_odd in (False, True):
        got = cpr_global(even, odd, newest_odd)
        assert got == pytest.approx((lat, lon), abs=2e-4)
    assert cpr_local(odd, True, lat + 0.5, lon - 0.5) == pytest.approx((lat, lon), abs=2e-4)
    surface = cpr_encode(lat, lon, True, surface=True)
    got = cpr_local(surface, True, lat + 0.1, lon + 0.1, surface=True)
    assert got == pytest.approx((lat, lon), abs=1e-4)


def test_cpr_global_refuses_a_pair_across_zones():
    # NL drops from 59 to 58 at 10.4705 degrees. Halves either side of it
    # were not sent close enough together to pair.
    assert nl(10.46) == 59 and nl(10.48) == 58
    assert cpr_global(cpr_encode(10.46, 5, False), cpr_encode(10.48, 5, True), False) is None


def test_nl_edges():
    assert nl(0) == 59
    assert nl(87) == 2
    assert nl(88) == 1
    assert nl(-45) == nl(45)


def test_altitude_and_squawk():
    assert altitude_ft(0) is None
    assert altitude_ft(0b110000111000) == 38000
    assert altitude_ft(0b110000101000) is None  # Q bit clear: Gillham, not decoded
    for code in ("1200", "7700", "0000", "7777", "4521"):
        assert squawk(id13(code)) == code


def test_movement():
    assert movement_kn(0) is None
    assert movement_kn(1) == 0.0
    assert movement_kn(2) == 0.125
    assert movement_kn(28) == 9.5
    assert movement_kn(124) == 175.0
    assert movement_kn(126) is None


def test_decode_refuses_what_it_should():
    assert decode_es(IDENT[:7]) is None
    # DF18 with control field 2, a TIS-B fine target: not decoded.
    head = bytes([(18 << 3) | 2]) + IDENT[1:11]
    assert decode_es(head + crc(head).to_bytes(3)) is None
