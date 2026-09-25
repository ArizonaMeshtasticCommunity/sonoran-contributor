"""APRS: KISS framing, AX.25, and the three position formats."""

import random
from collections import Counter

import pytest

from rf_agent.aprs import Direwolf, KissFramer, parse_ax25, parse_mic_e, parse_position
from rf_agent.messages import batch

from conftest import EPOCH, FIXTURES, assert_valid
from encode import ax25, kiss_frame, mic_e


def capture() -> bytes:
    return (FIXTURES / "aprs.kiss").read_bytes()


# The examples in the APRS 1.01 specification.
def test_uncompressed_position():
    got = parse_position(b"!4903.50N/07201.75W-Test 001234")
    assert got["lat"] == pytest.approx(49.05833, abs=1e-5)
    assert got["lon"] == pytest.approx(-72.02917, abs=1e-5)
    assert got["symbol"] == "/-"
    assert got["course"] is None and got["speed_kn"] is None


def test_position_with_a_timestamp_and_an_extension():
    got = parse_position(b"/092345z4903.50N/07201.75W>088/036/A=001234 moving")
    assert (got["lat"], got["lon"]) == pytest.approx((49.05833, -72.02917), abs=1e-5)
    assert got["course"] == 88 and got["speed_kn"] == 36
    assert got["alt_m"] == pytest.approx(1234 * 0.3048)


def test_compressed_position():
    got = parse_position(b"!/5L!!<*e7> sT")
    assert got["lat"] == pytest.approx(49.5, abs=1e-4)
    assert got["lon"] == pytest.approx(-72.75, abs=1e-4)
    # `sT` is a radio range, not a course and speed.
    assert got["course"] is None


@pytest.mark.parametrize(
    "lat,lon,speed,course",
    [
        (32.80, -113.30, 55, 200),
        (-33.87, 151.21, 0, 0),
        (51.50, -0.12, 120, 359),
        (33.00, 100.50, 7, 90),
        (0.05, -0.05, 250, 45),  # Mic-E stops at 799 knots
    ],
)
def test_mic_e_round_trip(lat, lon, speed, course):
    destination, info = mic_e(lat, lon, speed, course)
    got = parse_mic_e(destination, info)
    assert got["lat"] == pytest.approx(lat, abs=2e-4)
    assert got["lon"] == pytest.approx(lon, abs=2e-4)
    assert got["speed_kn"] == speed
    # APRS sends course 0 for "not known", so it is left off.
    assert got["course"] == (course if course else None)


def test_packets_without_a_position_are_not_points():
    for info in (
        b":KD7ABC   :hello there{001",
        b">Monitoring 146.520",
        b"T#005,199,000,255,073,123,01101001",
        b";FIELD DAY*111111z3248.00N/11318.00W-tent",
        b"",
    ):
        assert parse_position(info) is None


def test_ax25_addresses_and_frames_that_are_not_ui():
    frame = ax25("KD7ABC-9", "APRS", b"!3248.00N/11318.00W-x", digis=("WIDE1-1",))
    packet = parse_ax25(frame)
    assert (packet.source, packet.destination) == ("KD7ABC-9", "APRS")
    assert packet.info.startswith(b"!3248")
    assert parse_ax25(frame[:10]) is None
    not_ui = bytearray(frame)
    not_ui[21] = 0x00  # a control field that is not UI
    assert parse_ax25(bytes(not_ui)) is None


def test_framing_survives_noise_and_any_split():
    data = capture()
    whole = list(KissFramer().feed(data))
    rng = random.Random(11)
    framer = KissFramer()
    split, pos = [], 0
    while pos < len(data):
        size = rng.randint(1, 40)
        split += framer.feed(data[pos : pos + size])
        pos += size
    assert split == whole
    assert len(whole) >= 22


def test_escaped_bytes_survive():
    info = b"!3245.00N/11320.00W-edge \xc0 and \xdb here"
    frame = ax25("W1XYZ-5", "APRS", info)
    (got,) = list(KissFramer().feed(kiss_frame(frame)))
    assert parse_ax25(got).info == info


def test_the_capture_gives_one_point_per_position_packet():
    reader = Direwolf()
    points = reader.feed(capture(), EPOCH)
    per = Counter(p.id for p in points)
    assert per == {"kd7abc": 3, "w1xyz-5": 6, "kd7abc-9": 12}
    # Messages, status, telemetry and the object carry no position.
    assert reader.no_position == 4
    assert reader.bad == 0
    fixed = next(p for p in points if p.id == "kd7abc")
    assert fixed.alt_m == pytest.approx(787 * 0.3048, abs=1)
    assert fixed.meta["symbol"] == "/-"
    car = next(p for p in points if p.id == "kd7abc-9")
    assert car.gs_kn == 55 and car.track_deg == 200
    for start in range(0, len(points), 100):
        assert_valid(batch("test-site", "aprs", points[start : start + 100], EPOCH))


def test_bad_input_is_counted_not_raised():
    reader = Direwolf()
    rng = random.Random(5)
    for _ in range(50):
        reader.feed(rng.randbytes(rng.randint(1, 200)), EPOCH)
    reader.feed(kiss_frame(b"\x00" * 20), EPOCH)
    assert reader.bad >= 1
