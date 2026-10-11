from msdr.wifi import dot11
from tests.wifi.fixtures import (
    append_fcs,
    build_beacon_frame,
    build_rsn_ie,
    build_wpa1_ie_body,
)


def test_beacon_basic_fields():
    frame = build_beacon_frame(
        bssid="aa:bb:cc:dd:ee:01", capability=0x0001, ds_channel=6, frame_timestamp=42
    )
    result = dot11.parse(frame)
    assert isinstance(result, dot11.ManagementFrame)
    assert result.subtype == dot11.SUBTYPE_BEACON
    assert result.bssid == "aa:bb:cc:dd:ee:01"
    assert result.ssid_text == "TestNet"
    assert result.ssid_hidden is False
    assert result.ds_channel == 6
    assert result.cap_ess is True
    assert result.cap_privacy is False
    assert result.frame_timestamp == 42
    assert result.supported_rates_mbps == (1.0, 2.0, 5.5, 11.0)


def test_probe_response_subtype():
    frame = build_beacon_frame(subtype=5)
    result = dot11.parse(frame)
    assert result.subtype == dot11.SUBTYPE_PROBE_RESPONSE


def test_unsupported_subtype_is_error_value():
    frame = build_beacon_frame(subtype=4)  # probe request
    result = dot11.parse(frame)
    assert isinstance(result, dot11.Dot11Error)
    assert result.kind == "unsupported_frame"


def test_hidden_ssid_zero_length():
    frame = build_beacon_frame(ssid=b"")
    result = dot11.parse(frame)
    assert result.ssid_hidden is True
    assert result.ssid_text == ""


def test_hidden_ssid_all_zero_bytes():
    frame = build_beacon_frame(ssid=b"\x00" * 10)
    result = dot11.parse(frame)
    assert result.ssid_hidden is True


def test_non_utf8_ssid_escaped_with_flag():
    frame = build_beacon_frame(ssid=b"\xff\xfe\x00bad")
    result = dot11.parse(frame)
    assert result.ssid_hidden is False
    assert result.ssid_utf8_ok is False
    assert "\\x" in result.ssid_text


def test_fcs_stripped_when_present():
    frame = build_beacon_frame()
    framed = append_fcs(frame)
    result = dot11.parse(framed, fcs_present=True)
    assert isinstance(result, dot11.ManagementFrame)
    assert result.bssid == "aa:bb:cc:dd:ee:01"


def test_truncated_frame_is_error_not_exception():
    result = dot11.parse(b"\x80\x00\x00\x00")
    assert isinstance(result, dot11.Dot11Error)
    assert result.kind == "truncated"


def test_malformed_ie_length_is_error_not_exception():
    frame = build_beacon_frame()
    # Corrupt the SSID IE's length byte (frame[24+12+1]) to claim more bytes
    # than actually follow.
    idx = 24 + 12 + 1
    assert frame[idx] == len(b"TestNet")
    corrupted = bytearray(frame)
    corrupted[idx] = 200
    result = dot11.parse(bytes(corrupted))
    assert isinstance(result, dot11.Dot11Error)
    assert result.kind == "malformed_ie"


def test_open_network():
    frame = build_beacon_frame(capability=0x0001)
    result = dot11.parse(frame)
    assert result.security_summary == "Open"


def test_wep_network():
    frame = build_beacon_frame(capability=0x0011)  # ESS + privacy, no RSN/WPA1
    result = dot11.parse(frame)
    assert result.security_summary == "WEP"


def test_wpa2_psk():
    rsn = build_rsn_ie(akms=(2,))
    frame = build_beacon_frame(capability=0x0011, rsn=rsn)
    result = dot11.parse(frame)
    assert result.security_summary == "WPA2-PSK"
    assert result.rsn.mfpc is False
    assert result.rsn.mfpr is False


def test_wpa3_sae_with_mfpr():
    rsn = build_rsn_ie(akms=(8,), mfpc=True, mfpr=True)
    frame = build_beacon_frame(capability=0x0011, rsn=rsn)
    result = dot11.parse(frame)
    assert result.security_summary == "WPA3-SAE"
    assert result.rsn.mfpr is True
    assert result.rsn.mfpc is True


def test_mixed_wpa2_wpa3():
    rsn = build_rsn_ie(akms=(2, 8), mfpc=True, mfpr=False)
    frame = build_beacon_frame(capability=0x0011, rsn=rsn)
    result = dot11.parse(frame)
    assert result.security_summary == "WPA2/WPA3"


def test_wpa2_enterprise():
    rsn = build_rsn_ie(akms=(1,))
    frame = build_beacon_frame(capability=0x0011, rsn=rsn)
    result = dot11.parse(frame)
    assert result.security_summary == "WPA2-Enterprise"


def test_owe():
    rsn = build_rsn_ie(akms=(18,))
    frame = build_beacon_frame(capability=0x0011, rsn=rsn)
    result = dot11.parse(frame)
    assert result.security_summary == "OWE"


def test_unknown_akm_shown_as_oui_type():
    rsn = build_rsn_ie(akms=(99,), akm_oui=b"\x11\x22\x33")
    frame = build_beacon_frame(capability=0x0011, rsn=rsn)
    result = dot11.parse(frame)
    assert result.security_summary == "112233:99"


def test_wpa1_vendor_ie_psk():
    wpa1 = build_wpa1_ie_body(akms=(2,))
    frame = build_beacon_frame(capability=0x0011, wpa1=wpa1)
    result = dot11.parse(frame)
    assert result.wpa1 is not None
    assert result.security_summary == "WPA1-PSK"


def test_ht_vht_he_presence():
    frame = build_beacon_frame(ht=True, vht=True, he=True)
    result = dot11.parse(frame)
    assert result.ht_present is True
    assert result.vht_present is True
    assert result.he_present is True


def test_country_ie():
    frame = build_beacon_frame(country=b"US ")
    result = dot11.parse(frame)
    assert result.country == "US"


def test_ies_kept_as_id_extid_bytes():
    frame = build_beacon_frame(ds_channel=11)
    result = dot11.parse(frame)
    ds_ies = [ie for ie in result.ies if ie.id == 3]
    assert len(ds_ies) == 1
    assert ds_ies[0].ext_id is None
    assert ds_ies[0].data == b"\x0b"


def test_two_bssids_same_ssid_parse_independently():
    frame_a = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"SharedSSID")
    frame_b = build_beacon_frame(bssid="aa:bb:cc:dd:ee:02", ssid=b"SharedSSID")
    result_a = dot11.parse(frame_a)
    result_b = dot11.parse(frame_b)
    assert result_a.ssid_text == result_b.ssid_text == "SharedSSID"
    assert result_a.bssid != result_b.bssid


def test_short_wpa1_vendor_ie_returns_error_not_typeerror():
    frame = build_beacon_frame(
        bssid="aa:bb:cc:dd:ee:01", extra_ies=[(221, bytes.fromhex("0050f201"))]
    )
    assert isinstance(dot11.parse(frame), dot11.Dot11Error)
