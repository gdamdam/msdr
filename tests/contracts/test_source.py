"""Source/channel contract: sample-index clock, explicit discontinuities,
generation bump, span-bounded virtual tuning (plan §6)."""

from msdr.contracts.source import ChannelDescriptor, SourceDescriptor


def _src(**kw):
    base = dict(
        source_id="rec-1",
        mode="replay",
        sample_rate_hz=8_000_000,
        center_hz=100_000_000,
        total_samples=80_000_000,
    )
    base.update(kw)
    return SourceDescriptor(**base)


def test_capture_time_defaults_to_unknown_not_now():
    s = _src()
    assert s.capture_time is None
    assert s.elapsed_s == 0.0


def test_elapsed_is_sample_index_over_rate():
    s = _src(sample_index=4_000_000)
    assert s.elapsed_s == 0.5


def test_bump_increments_generation_and_records_discontinuity():
    s = _src(sample_index=123)
    s2 = s.bump("seek")
    assert s.generation == 0 and s2.generation == 1
    assert s2.discontinuities[-1].kind == "seek"
    assert s2.discontinuities[-1].at_sample == 123
    assert s2.discontinuities[-1].generation == 1


def test_gap_duration_stays_unknown_unless_given():
    s = _src().bump("gap")
    assert s.discontinuities[-1].gap_samples is None
    s = _src().bump("gap", gap_samples=4000)
    assert s.discontinuities[-1].gap_samples == 4000


def test_channel_fits_only_inside_recorded_span():
    s = _src()  # 96-104 MHz
    inside = ChannelDescriptor(
        channel_id="c",
        source_id="rec-1",
        frequency_hz=98_100_000,
        bandwidth_hz=200_000,
        demodulator="WFM",
    )
    edge = ChannelDescriptor(
        channel_id="c",
        source_id="rec-1",
        frequency_hz=103_950_000,
        bandwidth_hz=200_000,
        demodulator="WFM",
    )
    outside = ChannelDescriptor(
        channel_id="c",
        source_id="rec-1",
        frequency_hz=105_000_000,
        bandwidth_hz=12_500,
        demodulator="NFM",
    )
    assert inside.fits(s)
    assert not edge.fits(s)  # 104.05 MHz upper edge is past the span
    assert edge.fits(_src(center_hz=100_100_000))
    assert not outside.fits(s)
    assert inside.offset_hz(s) == -1_900_000


def test_channel_margin_tightens_fit():
    s = _src()
    ch = ChannelDescriptor(
        channel_id="c",
        source_id="rec-1",
        frequency_hz=96_150_000,
        bandwidth_hz=200_000,
        demodulator="WFM",
    )
    assert ch.fits(s)
    assert not ch.fits(s, margin_hz=100_000)
