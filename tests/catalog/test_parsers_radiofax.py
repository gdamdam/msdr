from msdr.catalog.parsers.radiofax import parse_radiofax


def test_parse_radiofax_entry_count():
    entries = parse_radiofax()
    assert len(entries) == 19  # 4+4+4+4+3 frequencies across 5 stations


def test_parse_radiofax_all_usb_daily_2400bw():
    entries = parse_radiofax()
    assert all(e.source == "noaa_radiofax" for e in entries)
    assert all(e.mode == "USB" and e.bandwidth_hz == 2400 for e in entries)
    assert all(e.days is None for e in entries)  # daily
    assert all(e.start_utc == "0000" and e.stop_utc == "2400" for e in entries)
    assert all(e.country == "USA" for e in entries)


def test_parse_radiofax_nmc_point_reyes_spot_check():
    entries = parse_radiofax()
    nmc = [e for e in entries if e.station.startswith("NMC")]
    assert len(nmc) == 4
    freqs = {e.frequency_hz for e in nmc}
    assert freqs == {4_346_000, 8_682_000, 12_786_000, 17_151_200}
    assert all(e.location == "Point Reyes, CA" for e in nmc)
    assert all("dial -1.9 kHz for USB" in e.notes for e in nmc)
    assert all("fax" in e.notes for e in nmc)


def test_parse_radiofax_kvm70_honolulu_spot_check():
    entries = parse_radiofax()
    kvm70 = [e for e in entries if e.station.startswith("KVM70")]
    assert len(kvm70) == 3
    freqs = {e.frequency_hz for e in kvm70}
    assert freqs == {9_982_500, 11_090_000, 16_135_000}
    assert all(e.location == "Honolulu, HI" for e in kvm70)
