import struct

from msdr.wifi import pcap


def test_round_trip_little_endian():
    reader = pcap.PcapStreamReader()
    stream = pcap.write_header(byte_order="<")
    stream += pcap.write_record(1000.5, b"hello", byte_order="<")
    stream += pcap.write_record(1001.25, b"world!", byte_order="<")
    records = reader.feed(stream)
    assert [r.data for r in records] == [b"hello", b"world!"]
    assert records[0].ts == 1000.5
    assert records[1].linktype == pcap.LINKTYPE_IEEE802_11_RADIOTAP
    assert reader.pending_bytes == 0


def test_round_trip_big_endian():
    reader = pcap.PcapStreamReader()
    stream = pcap.write_header(byte_order=">") + pcap.write_record(42.0, b"abc", byte_order=">")
    records = reader.feed(stream)
    assert [r.data for r in records] == [b"abc"]
    assert records[0].ts == 42.0


def test_nanosecond_magic_hand_rolled_big_endian():
    # Constructed by hand (not via write_header) so the reader's magic
    # handling is verified independently of the writer.
    import struct

    header = b"\xa1\xb2\x3c\x4d" + struct.pack(">HHiIII", 2, 4, 0, 0, 262144, 1)
    record = struct.pack(">IIII", 5, 123_456_789, 2, 2) + b"hi"
    reader = pcap.PcapStreamReader()
    records = reader.feed(header + record)
    assert len(records) == 1
    assert records[0].data == b"hi"
    assert records[0].ts == 5 + 123_456_789 / 1_000_000_000.0


def test_truncated_final_record_stays_pending():
    reader = pcap.PcapStreamReader()
    full = pcap.write_header() + pcap.write_record(1.0, b"0123456789")
    # Cut the stream mid payload.
    records = reader.feed(full[:-3])
    assert records == []
    assert reader.pending_bytes > 0
    # Feed the rest: the record completes.
    records = reader.feed(full[-3:])
    assert [r.data for r in records] == [b"0123456789"]
    assert reader.pending_bytes == 0


def test_feed_in_arbitrary_chunks():
    reader = pcap.PcapStreamReader()
    full = pcap.write_header() + pcap.write_record(1.0, b"aaa") + pcap.write_record(2.0, b"bbb")
    out = []
    for i in range(0, len(full), 7):
        out += reader.feed(full[i : i + 7])
    assert [r.data for r in out] == [b"aaa", b"bbb"]


def test_oversize_record_dropped_and_counted():
    reader = pcap.PcapStreamReader(max_record_size=16)
    stream = pcap.write_header(snaplen=262144)
    stream += pcap.write_record(1.0, b"x" * 100)  # oversize vs max_record_size
    stream += pcap.write_record(2.0, b"ok")
    records = reader.feed(stream)
    assert [r.data for r in records] == [b"ok"]
    assert reader.oversize_dropped == 1


def test_snaplen_from_header_enforced_even_under_max_record_size():
    reader = pcap.PcapStreamReader(max_record_size=65536)
    stream = pcap.write_header(snaplen=8)
    stream += pcap.write_record(1.0, b"y" * 20)  # exceeds header snaplen
    stream += pcap.write_record(2.0, b"ok")
    records = reader.feed(stream)
    assert [r.data for r in records] == [b"ok"]
    assert reader.oversize_dropped == 1


def test_read_file(tmp_path):
    path = tmp_path / "x.pcap"
    data = pcap.write_header() + pcap.write_record(1.0, b"abc") + pcap.write_record(2.0, b"def")
    path.write_bytes(data)
    records = pcap.read_file(path)
    assert [r.data for r in records] == [b"abc", b"def"]


def test_huge_incl_len_is_skipped_without_buffering():
    reader = pcap.PcapStreamReader()
    # Header claims a 4 GB record; its payload then trickles in.
    bad = struct.pack("<IIII", 1, 0, 0xFFFFFFF0, 0xFFFFFFF0)
    assert reader.feed(pcap.write_header(snaplen=0xFFFFFFFF) + bad) == []
    assert reader.oversize_dropped == 1
    reader.feed(b"z" * 100_000)
    assert reader.pending_bytes == 0
