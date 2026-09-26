"""APRS: KISS framing, AX.25, and the three position formats."""

import random
from collections import Counter

import pytest

from rf_agent.aprs import (
    Direwolf,
    KissFramer,
    parse_ax25,
    parse_mic_e,
    parse_position,
    read_tail,
)
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
    # The fixed station beacons the same packet three times in a row, and
    # two of those are dropped as repeats. The object it placed is a
    # thing of its own, named with a leading `;`.
    assert per == {"kd7abc": 1, "w1xyz-5": 6, "kd7abc-9": 12, ";field_day": 1}
    assert reader.repeats == 2
    # Messages, status and telemetry carry no position.
    assert reader.no_position == 3
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


# The packets below are the shapes seen in the first live data from a
# station, with positions changed.


def test_a_weather_station_does_not_move():
    # With the `_` symbol, `120/000` is wind, not a course and speed.
    got = parse_position(b"@251207z3206.21N/11056.22W_120/000g002t064P000r000b10140e1w")
    assert got["course"] is None and got["speed_kn"] is None
    assert got["weather"] == {
        "wind_dir_deg": 120,
        "wind_kn": 0.0,
        "gust_kn": 1.74,
        "temperature_f": 64,
        "rain_today_in": 0.0,
        "rain_1h_in": 0.0,
        "pressure_hpa": 1014.0,
        "wx_type": "e1w",
    }
    # The readings come in any order: this one writes P before r, and
    # `e1w` names the software and station, not something a person typed.
    assert got["comment"] is None


def test_a_comment_is_kept_whole():
    text = b"North Phoenix WX3in1 #YAESUGANG RF & iGate K7XYG-10 U=13.3V and more besides"
    got = parse_position(b"=3339.48N/11202.04W#" + text)
    assert got["comment"] == text.decode()
    assert parse_position(b"!3519.60N/11141.50W_SnowBowl")["comment"] == "SnowBowl"


def test_a_station_extension_is_not_a_course():
    got = parse_position(b"!3248.00N/11318.00W#PHG5132 wide coverage")
    assert got["course"] is None and got["speed_kn"] is None
    assert got["comment"] == "wide coverage"


def test_mic_e_carries_its_message_and_a_readable_comment():
    # A Yaesu names itself after the comment, not before it.
    info = bytes([0x60]) + b"(`:lpi>/" + b'"87}rolling_$'
    got = parse_mic_e("S3SWUQ", info)
    assert got["lat"] == pytest.approx(33.6252, abs=1e-4)
    assert got["alt_m"] == 396
    assert got["message"] == "In Service"
    # The altitude and the radio's own name are not what a person wrote.
    assert got["comment"] == "rolling"
    assert got["weather"]["device"] == "Yaesu FT1D"


def test_a_relayed_packet_is_not_heard_directly():
    frame = ax25("W7SA-12", "APRS", b"!3206.21N/11056.22W_x", digis=("WIDE1-1", "WIDE2-1"))
    packet = parse_ax25(frame)
    assert packet.path == ("WIDE1-1", "WIDE2-1") and packet.direct
    relayed = bytearray(frame)
    relayed[20] |= 0x80  # the first digipeater has repeated it
    packet = parse_ax25(bytes(relayed))
    assert packet.path == ("WIDE1-1*", "WIDE2-1") and not packet.direct


def test_meta_says_how_the_packet_reached_us():
    reader = Direwolf()
    frame = ax25("KD7ABC", "APRS", b"!3248.00N/11318.00W-home", digis=("WIDE2-1",))
    (point,) = reader.feed(kiss_frame(frame), EPOCH)
    assert point.meta["path"] == "WIDE2-1"
    assert point.meta["comment"] == "home"
    # A plain packet keeps its real destination; Mic-E has none worth showing.
    assert point.meta["destination"] == "APRS"
    destination, info = mic_e(32.8, -113.3, 30, 90)
    (point,) = reader.feed(kiss_frame(ax25("KD7ABC-9", destination, info)), EPOCH)
    assert "destination" not in point.meta and point.meta["message"]


def test_a_point_says_whether_it_was_heard_directly():
    heard = ax25("KD7ABC", "APRS", b"!3248.00N/11318.00W-home", digis=("WIDE2-1",))
    (point,) = Direwolf().feed(kiss_frame(heard), EPOCH)
    assert point.direct is True and point.via is None
    assert point.to_json()["direct"] is True
    relayed = bytearray(heard)
    relayed[20] |= 0x80  # WIDE2-1 has repeated it
    (point,) = Direwolf().feed(kiss_frame(bytes(relayed)), EPOCH)
    assert point.direct is False
    # An alias names nobody, so there is no station to credit.
    assert point.via is None and "via" not in point.to_json()


def test_a_value_too_long_to_send_whole_is_left_out_not_cut():
    # Sonoran drops all of meta over its limits, so an impossible comment
    # is dropped on its own here, and the rest of meta still goes.
    reader = Direwolf()
    huge = b"x" * 300
    frame = ax25("KD7ABC", "APRS", b"!3248.00N/11318.00W-" + huge)
    (point,) = reader.feed(kiss_frame(frame), EPOCH)
    assert "comment" not in point.meta
    assert point.meta["symbol"] == "/-"


def test_via_names_the_last_station_that_repeated_it():
    frame = ax25("KD7ABC", "APRS", b"!3248.00N/11318.00W-home", digis=("W7MOT-3", "WIDE2-1"))
    relayed = bytearray(frame)
    relayed[20] |= 0x80  # W7MOT-3 repeated it; WIDE2-1 has not
    (point,) = Direwolf().feed(kiss_frame(bytes(relayed)), EPOCH)
    assert point.direct is False and point.via == "w7mot-3"
    # When the last used hop is an alias, an earlier callsign is not who
    # we heard, so there is nobody to credit.
    both = bytearray(frame)
    both[20] |= 0x80
    both[27] |= 0x80
    (point,) = Direwolf().feed(kiss_frame(bytes(both)), EPOCH)
    assert point.via is None


def test_a_repeat_is_dropped_unless_the_later_copy_is_direct():
    reader = Direwolf()
    frame = ax25("KD7ABC", "APRS", b"!3248.00N/11318.00W-home", digis=("W7MOT-3",))
    relayed = bytearray(frame)
    relayed[20] |= 0x80
    assert len(reader.feed(kiss_frame(bytes(relayed)), EPOCH)) == 1
    # The same beacon, heard directly this time: it says something new
    # about what this receiver can hear.
    (point,) = reader.feed(kiss_frame(frame), EPOCH + 5)
    assert point.direct is True
    # A third copy says nothing new.
    assert reader.feed(kiss_frame(frame), EPOCH + 6) == []
    # After the window it is a new beacon again.
    assert len(reader.feed(kiss_frame(frame), EPOCH + 40)) == 1


def test_a_packet_from_the_internet_credits_the_gateway():
    inner = b"CW1234>APRS,TCPIP*,qAC,T2TEST:!3248.00N/11318.00W_t072"
    frame = ax25("K7XYG-10", "APRS", b"}" + inner)
    (point,) = Direwolf().feed(kiss_frame(frame), EPOCH)
    assert point.id == "cw1234"
    assert point.direct is False and point.via == "k7xyg-10"
    assert point.meta["third_party"] is True
    assert point.meta["temperature_f"] == 72


def test_an_object_is_its_own_thing_and_names_its_owner():
    frame = ax25("KD7ABC", "APRS", b";FIELD DAY*111111z3248.00N/11318.00W-tent")
    (point,) = Direwolf().feed(kiss_frame(frame), EPOCH)
    assert point.id == ";field_day"
    assert point.meta["owner"] == "kd7abc" and point.meta["object"] == "object"
    assert point.meta["comment"] == "tent"
    # A killed object says the thing is gone, not where it is.
    killed = ax25("KD7ABC", "APRS", b";FIELD DAY_111111z3248.00N/11318.00W-tent")
    assert Direwolf().feed(kiss_frame(killed), EPOCH) == []


def test_an_item_is_read_like_an_object():
    frame = ax25("KD7ABC", "APRS", b")REPEATER!3248.00N/11318.00Wr146.520")
    (point,) = Direwolf().feed(kiss_frame(frame), EPOCH)
    assert point.id == ";repeater" and point.meta["object"] == "item"


@pytest.mark.parametrize(
    "raw,blur",
    [
        (b"!3248.75N/11318.50W-exact", None),
        (b"!3248.7 N/11318.5 W-one digit", 185),
        (b"!3248.  N/11318.  W-two", 1852),
        (b"!32  .  N/113  .  W-four", 111120),
    ],
)
def test_a_blurred_position_says_how_blurred_it_is(raw, blur):
    got = parse_position(raw)
    assert got["precision_m"] == blur
    # The point sits in the middle of what the sender was willing to say.
    assert got["lat"] == pytest.approx(32.8125 if blur is None else got["lat"], abs=1e-4)
    assert 32.4 < got["lat"] < 33.0


# Tails from the first live APRS data, with positions changed. Each held
# something the reader used to leave in the comment.


def test_a_radio_names_itself_around_the_comment():
    # Byonics: `\'` before the position, `|3` after the comment.
    comment, extras, _ = read_tail(b"'\"7x}|$>%g'r|!w32!|3", mic_e=True)
    assert comment is None
    assert extras["device"] == "Byonics TinyTrak3"
    # Kenwood: `]` before, `=` after, and the text in between is kept whole.
    comment, extras, _ = read_tail(b"]Greetings from Arizona!=", mic_e=True)
    assert comment == "Greetings from Arizona!"
    assert extras["device"] == "Kenwood TM-D710"
    comment, extras, _ = read_tail(b"]Local WIDE1-1 Digi=", mic_e=True)
    assert comment == "Local WIDE1-1 Digi"


def test_telemetry_is_the_radio_talking_about_itself():
    comment, extras, _ = read_tail(b"|#+%F'b|!wpb!|3", mic_e=True)
    assert comment is None
    # Two base 91 characters a value: `#+` is 2 * 91 + 10.
    assert extras["telemetry_seq"] == 192
    assert extras["telemetry_1"] == 401 and extras["telemetry_2"] == 611


def test_dao_adds_the_precision_the_sender_sent():
    plain = parse_position(b"!3248.00N/11318.00W-here")
    # `!w32!` in base 91 adds a hundredth of a minute to each.
    with_dao = parse_position(b"!3248.00N/11318.00W-here!w32!")
    assert with_dao["comment"] == "here"
    assert with_dao["lat"] > plain["lat"] and with_dao["lon"] < plain["lon"]
    assert with_dao["lat"] - plain["lat"] == pytest.approx(0.01 / 60 / 91 * 18, abs=1e-8)
    # `!W32!` spells the extra digit out instead.
    spelled = parse_position(b"!3248.00N/11318.00W-here!W32!")
    assert spelled["lat"] - plain["lat"] == pytest.approx(3 / 1000 / 60, abs=1e-9)
    assert spelled["comment"] == "here"


def test_a_station_with_no_sensors_reports_no_readings():
    got = parse_position(b"!3519.60N/11141.50W_.../...g...t...r000p000P000h..b00000.DsVP")
    # Wind, gust, temperature and humidity are absent, not zero.
    assert set(got["weather"]) == {"rain_1h_in", "rain_24h_in", "rain_today_in", "wx_type"}
    # A barometer reading zero is a station with no barometer.
    assert "pressure_hpa" not in got["weather"]
    assert got["weather"]["wx_type"] == ".DsVP"
    assert got["comment"] is None


def test_a_digipeater_keeps_what_it_wrote():
    # The New-N note after PHG is what the station wrote, so it stays.
    got = parse_position(b"!3248.00N/11318.00W#PHG5760/W2,AZn/Guadalupe Peak")
    assert got["comment"] == "/W2,AZn/Guadalupe Peak"
