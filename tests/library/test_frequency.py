from msdr.library.frequency import format_readout, parse_frequency


def test_decimal_mhz():
    assert parse_frequency("137.9") == 137_900_000
    assert parse_frequency("137.900") == 137_900_000


def test_explicit_suffix():
    assert parse_frequency("137900k") == 137_900_000
    assert parse_frequency("14.230 MHz") == 14_230_000


def test_bare_integer_thresholds():
    assert parse_frequency("10000000") == 10_000_000
    assert parse_frequency("7") == 7_000_000


def test_invalid_returns_none():
    assert parse_frequency("abc") is None


def test_format_readout():
    assert format_readout(137_900_000) == "137.900.000"
