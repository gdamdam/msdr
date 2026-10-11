import struct

from msdr.wifi import radiotap
from tests.wifi.fixtures import OUI_MSDR_RAW, build_radiotap


def test_tsft_flags_rate_channel_vendor_extended_bitmap():
    rt = build_radiotap(
        tsft=123456789,
        flags=radiotap.FLAG_FCS_AT_END,
        rate_500kbps=2,
        channel_mhz=2437,
        channel_flags=0x00A0,
        vendor=(OUI_MSDR_RAW, 0, b"\x7f\x01"),
        vendor_in_extended=True,
    )
    result = radiotap.parse(rt)
    assert not isinstance(result, radiotap.ParseError)
    assert result.tsft == 123456789
    assert result.flags.fcs_present is True
    assert result.flags.bad_fcs is False
    assert result.rate_500kbps == 2
    assert result.channel_mhz == 2437
    assert result.channel_flags == 0x00A0
    assert len(result.present_words) == 2  # chained: vendor lived in word 1
    assert result.vendor[(OUI_MSDR_RAW, 0)] == b"\x7f\x01"
    assert result.header_length == len(rt)


def test_bad_fcs_flag():
    rt = build_radiotap(flags=radiotap.FLAG_FCS_AT_END | radiotap.FLAG_BAD_FCS)
    result = radiotap.parse(rt)
    assert result.flags.fcs_present is True
    assert result.flags.bad_fcs is True


def test_dbm_antsignal():
    rt = build_radiotap(antsignal=-71)
    result = radiotap.parse(rt)
    assert result.dbm_antsignal == -71


def test_vendor_namespace_without_extended_bitmap():
    rt = build_radiotap(vendor=(b"\x00\x00\x00", 0, b"\x01\x02\x03"))
    result = radiotap.parse(rt)
    assert len(result.present_words) == 1
    assert result.vendor[(b"\x00\x00\x00", 0)] == b"\x01\x02\x03"


def test_alignment_tsft_then_channel_offsets():
    # tsft (8 bytes, align 8) then channel (4 bytes, align 2): channel must
    # sit right after tsft at offset 8+8=16 with no extra padding needed.
    rt = build_radiotap(tsft=1, channel_mhz=5180, channel_flags=0x0140)
    result = radiotap.parse(rt)
    assert result.tsft == 1
    assert result.channel_mhz == 5180
    assert result.channel_flags == 0x0140


def test_unknown_field_bit_stops_parsing_safely():
    # Hand-build a header with a present bit (28, reserved/unsized) set.
    present = 1 << 28
    header = struct.pack("<BBH", 0, 0, 8) + struct.pack("<I", present)
    result = radiotap.parse(header)
    assert isinstance(result, radiotap.ParseError)
    assert result.kind == "unknown_field"


def test_truncated_header_returns_error():
    result = radiotap.parse(b"\x00\x00")
    assert isinstance(result, radiotap.ParseError)
    assert result.kind == "truncated"


def test_declared_length_past_buffer_is_error():
    header = struct.pack("<BBH", 0, 0, 999) + struct.pack("<I", 0)
    result = radiotap.parse(header)
    assert isinstance(result, radiotap.ParseError)
    assert result.kind == "truncated"


def test_bad_version_is_error():
    header = struct.pack("<BBH", 1, 0, 8) + struct.pack("<I", 0)
    result = radiotap.parse(header)
    assert isinstance(result, radiotap.ParseError)
    assert result.kind == "bad_version"


def test_header_with_no_fields_present():
    rt = build_radiotap()
    result = radiotap.parse(rt)
    assert result.tsft is None
    assert result.flags is None
    assert result.header_length == 8


def test_vendor_namespace_word_fields_are_not_read_as_radiotap_fields():
    """Header bytes copied from a real msdr-wifi-capture record (AWUS036ACH, ch 6):
    TSFT+flags+rate+channel, vendor namespace (bit 30) and a second presence word
    whose bit 0 is a vendor field. The second word must not be read as TSFT."""
    from msdr.wifi.radiotap import ParseError, parse

    header = bytes.fromhex(
        "000044000f0000c00100000000000000ed950a0000000000100c8509c0000000000020002a2a00a8"
    )
    # Pad to the declared 68-byte header length (vendor payload bytes are opaque).
    data = header + bytes(68 - len(header)) + b"\x80\x00"
    rt = parse(data)
    assert not isinstance(rt, ParseError), rt
    assert rt.header_length == 68
    assert rt.channel_mhz == 2437
    assert rt.flags.fcs_present is True
    assert rt.tsft == 0x0A95ED


def test_chained_presence_words_keep_first_chain_antsignal():
    # word0: antsignal + radiotap-NS-next + more; word1: antsignal (chain 1).
    word0 = (1 << 5) | (1 << 29) | (1 << 31)
    word1 = 1 << 5
    hdr = struct.pack("<BBHII", 0, 0, 14, word0, word1) + struct.pack("<bb", -40, -70)
    rt = radiotap.parse(hdr)
    assert not isinstance(rt, radiotap.ParseError)
    assert rt.dbm_antsignal == -40
