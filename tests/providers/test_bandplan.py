import json
from importlib import resources

from msdr.library.models import CATEGORY_NAMES
from msdr.providers.bandplan import BandPlanProvider
from msdr.providers.base import IdentifyQuery


def identify(hz, mode=None, bw=None, region="US"):
    return BandPlanProvider(region).identify(
        IdentifyQuery(frequency_hz=hz, demodulator=mode, filter_bandwidth_hz=bw, region=region)
    )


def test_wx1_top_candidate():
    # Real channel numbering is not ascending by frequency: WX1 = 162.550 MHz.
    candidates = identify(162_550_000, "NFM", 12500)
    assert candidates
    top = candidates[0]
    assert "WX1" in top.name
    assert top.confidence >= 0.9
    assert top.category == "bc"


def test_wx7_is_162_525():
    candidates = identify(162_525_000, "NFM", 12500)
    assert candidates
    assert "WX7" in candidates[0].name


def test_iss_repeater_downlink_beats_generic_leo_band():
    # 437.800 MHz is the ISS cross-band repeater output; the generic LEO band
    # still appears, but the exact channel must rank first.
    candidates = identify(437_800_000, "NFM", 12500)
    assert candidates
    assert "ISS" in candidates[0].name
    assert candidates[0].category == "sat"
    assert any("LEO" in c.name for c in candidates)


def test_meteor_top_noaa_apt_present():
    candidates = identify(137_900_000, "NFM", 120_000)
    assert candidates[0].name.startswith("Meteor")
    assert any("NOAA" in c.name for c in candidates)


def test_2m_calling_top_and_band_present():
    candidates = identify(146_520_000)
    assert candidates[0].name == "2 m calling"
    assert any("2 m" in c.name and c.name != "2 m calling" for c in candidates)


def test_aviation_guard_top():
    candidates = identify(121_500_000, "AM")
    assert candidates[0].name == "Aviation guard/emergency"


def test_no_match_is_empty():
    assert identify(1) == []


def test_land_mobile_plus_digital_hint():
    candidates = identify(154_000_000, "NFM", 12500)
    names = [c.name for c in candidates]
    assert any("Land Mobile" in n for n in names)
    hint = next(c for c in candidates if c.name == "Digital voice (DMR/P25/NXDN?)")
    assert hint.confidence == 0.2
    assert hint.category == "digital"


def test_other_region_is_empty_no_crash():
    assert identify(146_520_000, region="EU") == []


def test_digital_hint_suppressed_on_exact_channel_hit():
    # 162.550 MHz is both NOAA WX1 (channel, >=0.9) and inside the broad
    # land-mobile VHF band -- the generic hint would just be noise here.
    candidates = identify(162_550_000, "NFM", 12500)
    assert not any(c.name == "Digital voice (DMR/P25/NXDN?)" for c in candidates)


def test_digital_hint_still_fires_without_a_strong_channel_hit():
    candidates = identify(154_000_000, "NFM", 12500)
    assert any(c.name == "Digital voice (DMR/P25/NXDN?)" for c in candidates)


def test_bandplan_json_schema():
    text = resources.files("msdr.providers.data").joinpath("bandplan_us.json").read_text()
    entries = json.loads(text)
    assert 100 <= len(entries) <= 140
    required_keys = {"name", "lo_hz", "hi_hz", "modes", "bw_hz", "category", "kind", "note"}
    allowed_categories = set(CATEGORY_NAMES)
    allowed_modes = {"AM", "USB", "LSB", "CW", "SAM", "NFM", "WFM"}
    for entry in entries:
        assert required_keys <= set(entry.keys())
        assert entry["lo_hz"] <= entry["hi_hz"]
        assert entry["kind"] in ("band", "channel")
        assert entry["category"] is None or entry["category"] in allowed_categories
        if entry["kind"] == "channel":
            assert entry["lo_hz"] == entry["hi_hz"]
        if entry["modes"] is not None:
            assert set(entry["modes"]) <= allowed_modes


def test_off_center_1090_has_aviation_allocation_without_claiming_adsb():
    candidates = identify(1_089_974_844, 'NFM', 12500)
    assert candidates[0].category == 'air'
    assert 'Aviation radionavigation' in candidates[0].name
    assert not any(c.name == 'ADS-B 1090ES' for c in candidates)
    assert identify(1_090_000_000)[0].name == 'ADS-B 1090ES'
