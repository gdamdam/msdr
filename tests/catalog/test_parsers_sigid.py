from msdr.catalog.parsers.sigid import parse_sigid


def test_parse_sigid_row_count_in_range():
    entries = parse_sigid()
    assert 30 <= len(entries) <= 40


def test_parse_sigid_all_have_wiki_url_notes():
    entries = parse_sigid()
    assert all(e.source == "sigidwiki" for e in entries)
    assert all(e.notes and e.notes.startswith("https://www.sigidwiki.com/wiki/") for e in entries)


def test_parse_sigid_single_frequency_has_zero_bandwidth():
    entries = parse_sigid()
    navtex = next(e for e in entries if e.frequency_hz == 518_000)
    assert navtex.bandwidth_hz == 0  # lo == hi convention
    assert "NAVTEX" in navtex.notes


def test_parse_sigid_range_has_nonzero_bandwidth_centred():
    entries = parse_sigid()
    ft8_20m = next(e for e in entries if e.station == "FT8 (20m)")
    assert ft8_20m.frequency_hz == 14_074_000
    assert ft8_20m.bandwidth_hz == 0  # single dial frequency

    fax = next(e for e in entries if "WEFAX" in e.station)
    assert fax.frequency_hz == (4_000_000 + 13_600_000) // 2
    assert fax.bandwidth_hz == 13_600_000 - 4_000_000
