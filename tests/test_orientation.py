"""Pin the facing convention. The four cardinal rows are the contract: raw grows clockwise
seen from above, 0 is south and 8192 is north. If a live check moves the zero offset,
NORTH_RAW changes and these rows say what must follow."""
import pytest

from botwithus.api import orientation as O
from botwithus.api.orientation import Direction


@pytest.mark.parametrize("raw, bearing, point", [
    (8192, 0.0, Direction.NORTH),
    (12288, 90.0, Direction.EAST),
    (0, 180.0, Direction.SOUTH),
    (4096, 270.0, Direction.WEST),
])
def test_cardinals(raw, bearing, point):
    assert O.degrees(raw) == pytest.approx(bearing)
    assert O.compass(raw) is point


@pytest.mark.parametrize("raw, point", [
    (10240, Direction.NORTH_EAST),
    (14336, Direction.SOUTH_EAST),
    (2048, Direction.SOUTH_WEST),
    (6144, Direction.NORTH_WEST),
])
def test_intercardinals(raw, point):
    assert O.compass(raw) is point


def test_just_short_of_north_wraps_to_north():
    raw = O.NORTH_RAW - 1
    assert O.degrees(raw) > Direction.NORTH_WEST.degrees
    assert O.compass(raw) is Direction.NORTH


def _fail_on_report(value):
    raise AssertionError(f"an in-contract wire value was reported: {value}")


def test_wire_sentinel_is_unknown_with_no_bearing_and_no_point():
    raw = O.from_wire(O.WIRE_UNKNOWN, _fail_on_report)
    assert raw == O.UNKNOWN_RAW
    assert not O.is_known(raw)
    assert O.degrees(raw) is None
    assert O.compass(raw) is None


def test_wire_angle_decodes_to_itself():
    assert O.from_wire(O.NORTH_RAW, _fail_on_report) == O.NORTH_RAW


@pytest.mark.parametrize("bad", [O.FULL_TURN, 0xFFFE, -2])
def test_from_wire_degrades_an_out_of_contract_value_to_unknown_and_reports_it(bad):
    # Degrade at the wire: one bad row must not raise out of a snapshot decode.
    reported = []
    assert O.from_wire(bad, reported.append) == O.UNKNOWN_RAW
    assert reported == [bad]


@pytest.mark.parametrize("bad", [O.FULL_TURN, 0xFFFE, -2])
def test_api_raises_for_an_out_of_range_raw(bad):
    # Raise at the API: handing a bad raw to the converter is the caller's mistake.
    with pytest.raises(ValueError):
        O.degrees(bad)


def test_wire_decoder_reports_only_the_first_out_of_contract_value():
    reported = []
    decoder = O.WireOrientationDecoder(reported.append)
    for _ in range(50):
        assert decoder.decode(0xFFFE) == O.UNKNOWN_RAW
    assert decoder.decode(O.FULL_TURN) == O.UNKNOWN_RAW
    assert decoder.decode(O.NORTH_RAW) == O.NORTH_RAW
    assert reported == [0xFFFE]


def test_wire_decoder_default_report_logs_one_warning(caplog):
    decoder = O.WireOrientationDecoder()
    with caplog.at_level("WARNING"):
        decoder.decode(0xFFFE)
        decoder.decode(0xFFFE)
    assert len(caplog.records) == 1


def test_is_same_facing_allows_one_unit_of_read_back():
    assert O.is_same_facing(O.NORTH_RAW - O.READBACK_TOLERANCE, O.NORTH_RAW)
    assert O.NORTH_RAW - 1 != O.NORTH_RAW  # which is why == must not be used for facing


def test_is_same_facing_wraps_around_the_turn():
    assert O.is_same_facing(O.FULL_TURN - 1, 0)


def test_is_same_facing_two_units_apart_is_different():
    assert not O.is_same_facing(O.NORTH_RAW - 2, O.NORTH_RAW)


def test_is_same_facing_unknown_matches_nothing():
    assert not O.is_same_facing(O.UNKNOWN_RAW, O.UNKNOWN_RAW)
    assert not O.is_same_facing(O.UNKNOWN_RAW, O.NORTH_RAW)
