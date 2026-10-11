from msdr.recorder.ring import SegmentRing


def test_evict_keeps_newest_beyond_preroll(tmp_path):
    ring = SegmentRing(tmp_path / "preroll")
    for i in range(5):
        ring.open_segment(sample_rate=1, center_hz=100, settings={}, now=1000.0 + i)
        ring.write(b"\x00\x00\x00\x00")  # 1 complex sample -> 1s of buffer at rate=1
        ring.close_segment()

    ring.evict(keep_s=2)

    remaining = ring.closed_segments()
    assert len(remaining) == 2
    assert sum(m.samples for m in remaining) == 2
    # keeps the newest two segments (opened at now=1003 and now=1004)
    assert [m.start_wall for m in remaining] == [1003.0, 1004.0]


def test_evict_never_leaves_less_than_preroll(tmp_path):
    ring = SegmentRing(tmp_path / "preroll")
    for i in range(4):
        ring.open_segment(sample_rate=2, center_hz=100, settings={}, now=1000.0 + i)
        ring.write(b"\x00" * 12)  # 3 samples at 2 S/s -> 1.5 s each
        ring.close_segment()
    ring.evict(keep_s=2)
    assert ring.buffered_s() == 3.0  # two segments; one alone (1.5 s) would fall short


def test_evict_never_touches_open_segment(tmp_path):
    ring = SegmentRing(tmp_path / "preroll")
    ring.open_segment(sample_rate=1, center_hz=100, settings={}, now=0.0)
    ring.write(b"\x00\x00\x00\x00")
    # Not closed -- evict only considers closed_segments(), so this must survive.
    ring.evict(keep_s=0)
    assert ring.current is not None
    ring.close_segment()  # avoid leaking the open file handle past the test
