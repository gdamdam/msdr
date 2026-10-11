from msdr.catalog.parsers.eibi import default_url, parse_eibi, season_from_filename

EIBI_FIXTURE = """\
kHz;Time (UTC);Days;ITU;Station;Lng;Target;Remarks;P;Start;Stop
6070;2200-0600;;D;Deutschlandfunk;G;Europe;midnight-wrap test;100;;
9410;1000-1057;Mo-Fr;CHN;China Radio Intl;C;Asia;weekday only;;;;
6000;;;C;Radio Habana Cuba;S;Am;24h broadcast, always on;;;;
"""


def test_parse_eibi_basic_fields():
    entries = parse_eibi(EIBI_FIXTURE, filename="sked-b25.csv")
    assert len(entries) == 3
    assert all(e.source == "eibi" and e.mode == "AM" and e.bandwidth_hz == 9000 for e in entries)
    assert all(e.season == "B25" for e in entries)


def test_parse_eibi_midnight_wrapping_window():
    entries = parse_eibi(EIBI_FIXTURE)
    dlf = next(e for e in entries if e.station == "Deutschlandfunk")
    assert dlf.start_utc == "2200"
    assert dlf.stop_utc == "0600"
    assert dlf.days is None  # empty Days column = daily


def test_parse_eibi_weekday_mask():
    entries = parse_eibi(EIBI_FIXTURE)
    crc = next(e for e in entries if e.station == "China Radio Intl")
    assert crc.days == "MTWTF.."  # Mo-Fr


def test_parse_eibi_always_on_when_time_and_days_blank():
    entries = parse_eibi(EIBI_FIXTURE)
    habana = next(e for e in entries if "Habana" in e.station)
    assert habana.start_utc is None
    assert habana.stop_utc is None
    assert habana.country == "C"
    assert habana.language == "S"


def test_season_from_filename():
    assert season_from_filename("sked-b25.csv") == "B25"
    assert season_from_filename("sked-A26.csv") == "A26"
    assert season_from_filename("garbage.txt") is None


def test_default_url():
    assert default_url("B25") == "http://www.eibispace.de/dx/sked-b25.csv"


def test_day_digit_helpers_ignore_out_of_range_digits_and_terminate():
    from msdr.catalog.parsers._days import digit_ranges_to_mask, digits_to_mask

    assert digits_to_mask("8") is None
    assert digits_to_mask("08") is None  # 0 must not become Sunday
    assert digits_to_mask("18") == "M......"
    assert digit_ranges_to_mask("1-9") is None  # used to loop forever
    assert digit_ranges_to_mask("1-9 3") == "..W...."
