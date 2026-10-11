import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from msdr.providers.tracking import (
    EPHEMERIS_NAME,
    SatTracker,
    TleStore,
    fetch_ephemeris,
    load_cached_ephemeris,
)
from msdr.settings import Location

# Pinned ISS OMM (CelesTrak GP JSON), epoch in 2026 so it stays valid for
# these tests regardless of when they run.
ISS_OMM = {
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "EPOCH": "2026-09-22T06:30:37.496448",
    "MEAN_MOTION": 15.49224498,
    "ECCENTRICITY": 0.00047657,
    "INCLINATION": 51.6312,
    "RA_OF_ASC_NODE": 179.6046,
    "ARG_OF_PERICENTER": 167.6102,
    "MEAN_ANOMALY": 192.5004,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 25544,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 58680,
    "BSTAR": 0.0001364276,
    "MEAN_MOTION_DOT": 7.132e-05,
    "MEAN_MOTION_DDOT": 0,
}
ISS_EPOCH = datetime(2026, 9, 22, 6, 30, 37, tzinfo=UTC)
SAN_FRANCISCO = Location(lat=37.77, lon=-122.42, altitude_m=15.0, timezone="America/Los_Angeles")


def _store_with(tmp_path: Path, norad: int = 25544, omm: dict = ISS_OMM) -> TleStore:
    tle_dir = tmp_path / "tle"
    tle_dir.mkdir()
    (tle_dir / f"{norad}.json").write_text(json.dumps([omm]))
    return TleStore([tle_dir])


def test_position_at_epoch_returns_finite_az_el_range(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    result = tracker.position("ISS", at=ISS_EPOCH)

    assert result.error is None
    pos = result.position
    assert pos is not None
    assert -90 <= pos.elevation_deg <= 90
    assert 0 <= pos.azimuth_deg < 360
    assert pos.range_km > 0
    assert result.tle_age_days is not None and result.tle_age_days >= 0


def test_track_produces_monotonic_times_and_valid_elevations(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    track = tracker.track("ISS", ISS_EPOCH, ISS_EPOCH + timedelta(hours=2), step_s=60)

    assert track is not None
    assert len(track.points) > 1
    times = [datetime.fromisoformat(p.at.replace("Z", "+00:00")) for p in track.points]
    assert times == sorted(times)
    assert times[0] < times[-1]
    assert all(p.elevation_deg <= 90 for p in track.points)


def test_next_or_current_pass_returns_pass_above_horizon(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    # Pinned to the fixture's own epoch: searching from "now" made this
    # depend on the wall clock (and on how far the elements had drifted).
    pass_ = tracker.next_or_current_pass("ISS", horizon_deg=0.0, hours=24, start=ISS_EPOCH)

    assert pass_ is not None
    rise = datetime.fromisoformat(pass_.rise.replace("Z", "+00:00"))
    set_ = datetime.fromisoformat(pass_.set.replace("Z", "+00:00"))
    assert ISS_EPOCH <= rise < ISS_EPOCH + timedelta(hours=24)
    assert pass_.max_elevation_deg > 0.0
    assert pass_.rise < pass_.peak < pass_.set
    assert set_ > rise


def test_doppler_hz_changes_sign_across_the_peak(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)
    pass_ = tracker.next_or_current_pass("ISS", horizon_deg=0.0, hours=24, start=ISS_EPOCH)
    assert pass_ is not None
    peak_at = datetime.fromisoformat(pass_.peak.replace("Z", "+00:00"))

    before = tracker.doppler_hz("ISS", 145_800_000, at=peak_at - timedelta(minutes=2))
    after = tracker.doppler_hz("ISS", 145_800_000, at=peak_at + timedelta(minutes=2))

    assert (before > 0) != (after > 0)


def test_missing_tle_returns_error_string(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    tracker = SatTracker(TleStore([empty_dir]), SAN_FRANCISCO)

    result = tracker.position("ISS", at=ISS_EPOCH)

    assert result.position is None
    assert isinstance(result.error, str) and result.error


def test_incomplete_cached_omm_returns_actionable_error(tmp_path):
    # A cache entry with an epoch but missing orbital fields (e.g. a partial
    # write, or an older cache format) must not raise a bare KeyError out of
    # EarthSatellite.from_omm -- every other _resolve failure comes back as
    # an error string, so this one must too.
    incomplete = {k: v for k, v in ISS_OMM.items() if k != "MEAN_MOTION"}
    tracker = SatTracker(_store_with(tmp_path, omm=incomplete), SAN_FRANCISCO)

    result = tracker.position("ISS", at=ISS_EPOCH)

    assert result.position is None
    assert isinstance(result.error, str)
    assert "incomplete" in result.error
    assert "ISS" in result.error


def test_missing_location_returns_error_but_keeps_tle_age(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), None)

    result = tracker.position("ISS", at=ISS_EPOCH)

    assert result.position is None
    assert result.error == "no location configured"
    assert result.tle_age_days is not None and result.tle_age_days >= 0


def test_unknown_satellite_label_returns_error(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    result = tracker.position("NOT-A-SAT", at=ISS_EPOCH)

    assert result.position is None
    assert "unknown satellite" in result.error


def test_tle_store_prefers_newest_epoch_across_two_dirs(tmp_path):
    older_dir = tmp_path / "older"
    newer_dir = tmp_path / "newer"
    older_dir.mkdir()
    newer_dir.mkdir()
    older = {**ISS_OMM, "EPOCH": "2026-09-10T00:00:00"}
    newer = {**ISS_OMM, "EPOCH": "2026-09-21T00:00:00"}
    (older_dir / "25544.json").write_text(json.dumps([older]))
    (newer_dir / "25544.json").write_text(json.dumps([newer]))

    # older dir listed first: load() must still pick the newer EPOCH.
    store = TleStore([older_dir, newer_dir])

    loaded = store.load(25544)
    assert loaded["EPOCH"] == newer["EPOCH"]


# --- SatTracker.passes() (unified PASSES, internal engine) ------------------


def test_passes_over_24h_returns_pass_with_right_shape_and_local_timezone(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    passes = tracker.passes(
        ["ISS"], days=1, min_elevation=0.0, horizon=0.0, top=50, start=ISS_EPOCH
    )

    assert len(passes) >= 1
    p = passes[0]
    assert p.satellite == "ISS"
    assert p.norad == 25544
    assert p.rise < p.peak < p.set
    assert p.max_elevation_deg >= 0.0
    assert p.geometry in ("Excellent", "Good", "Fair", "Low")
    assert p.rank == 1
    assert p.window_minutes > 0
    assert p.range_at_peak_km > 0
    # Local timezone (America/Los_Angeles, UTC-7 in September), not bare UTC.
    for field in (p.rise, p.peak, p.set):
        assert datetime.fromisoformat(field).utcoffset() == timedelta(hours=-7)


def test_passes_filters_by_min_elevation_and_caps_at_top(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    unfiltered = tracker.passes(
        ["ISS"], days=3, min_elevation=0.0, horizon=0.0, top=50, start=ISS_EPOCH
    )
    filtered = tracker.passes(
        ["ISS"], days=3, min_elevation=80.0, horizon=0.0, top=50, start=ISS_EPOCH
    )
    capped = tracker.passes(["ISS"], days=3, min_elevation=0.0, horizon=0.0, top=1, start=ISS_EPOCH)

    assert len(filtered) <= len(unfiltered)
    assert all(p.max_elevation_deg >= 80.0 for p in filtered)
    assert len(capped) <= 1


def test_passes_skips_unknown_and_tleless_labels(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    tracker = SatTracker(TleStore([empty_dir]), SAN_FRANCISCO)

    passes = tracker.passes(["NOT-A-SAT"], days=1, min_elevation=0.0, horizon=0.0, top=10)

    assert passes == []


def test_passes_returns_empty_without_location(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), None)

    passes = tracker.passes(["ISS"], days=1, min_elevation=0.0, horizon=0.0, top=10)

    assert passes == []


# --- extra (user-added) satellites, by NORAD id -----------------------------


def test_extra_satellite_resolves_position_by_norad(tmp_path):
    tracker = SatTracker(
        _store_with(tmp_path), SAN_FRANCISCO, extra={"MYSAT": {"label": "MYSAT", "norad": 25544}}
    )

    result = tracker.position("MYSAT", at=ISS_EPOCH)

    assert result.position is not None
    assert result.position.norad == 25544


def test_extra_satellite_included_in_passes(tmp_path):
    tracker = SatTracker(
        _store_with(tmp_path), SAN_FRANCISCO, extra={"MYSAT": {"label": "MYSAT", "norad": 25544}}
    )

    passes = tracker.passes(
        ["MYSAT"], days=1, min_elevation=0.0, horizon=0.0, top=10, start=ISS_EPOCH
    )

    assert len(passes) >= 1
    assert passes[0].satellite == "MYSAT"
    assert passes[0].norad == 25544


# --- passes_visible_only: ephemeris-gated visibility -----------------------


def _fake_annotate(visible):
    def annotate(self, p, sat, peak_t, obs, eph):
        p.visible = visible

    return annotate


def test_passes_visible_only_drops_non_visible(tmp_path, monkeypatch):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)
    monkeypatch.setattr(SatTracker, "_annotate_visible", _fake_annotate(False))

    passes = tracker.passes(
        ["ISS"],
        days=3,
        min_elevation=0.0,
        horizon=0.0,
        top=50,
        start=ISS_EPOCH,
        ephemeris=object(),
        visible_only=True,
    )

    assert passes == []


def test_passes_visible_only_keeps_visible(tmp_path, monkeypatch):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)
    monkeypatch.setattr(SatTracker, "_annotate_visible", _fake_annotate(True))

    passes = tracker.passes(
        ["ISS"],
        days=3,
        min_elevation=0.0,
        horizon=0.0,
        top=50,
        start=ISS_EPOCH,
        ephemeris=object(),
        visible_only=True,
    )

    assert len(passes) >= 1
    assert all(p.visible for p in passes)


def test_passes_without_ephemeris_leaves_visible_none_even_if_visible_only(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)

    passes = tracker.passes(
        ["ISS"],
        days=1,
        min_elevation=0.0,
        horizon=0.0,
        top=50,
        start=ISS_EPOCH,
        ephemeris=None,
        visible_only=True,
    )

    # Fail open: with no ephemeris cached yet, nothing is hidden.
    assert len(passes) >= 1
    assert all(p.visible is None for p in passes)


# --- de421.bsp cache/fetch (passes_visible_only's ephemeris) ----------------


def test_load_cached_ephemeris_missing_file_is_none(tmp_path):
    assert load_cached_ephemeris(tmp_path) is None


def test_load_cached_ephemeris_corrupt_file_degrades_to_none(tmp_path):
    (tmp_path / EPHEMERIS_NAME).write_bytes(b"not a real bsp file")
    assert load_cached_ephemeris(tmp_path) is None


async def test_fetch_ephemeris_downloads_via_skyfield_loader(tmp_path, monkeypatch):
    calls = []

    class _FakeLoader:
        def __init__(self, directory):
            calls.append(directory)

        def __call__(self, name):
            calls.append(name)

    import skyfield.api

    monkeypatch.setattr(skyfield.api, "Loader", _FakeLoader)

    await fetch_ephemeris(tmp_path)

    assert calls == [str(tmp_path), EPHEMERIS_NAME]


def test_visibility_includes_sunlit_portion_before_peak(tmp_path):
    """A shadowed culmination must not hide the sunlit start of a pass."""
    from types import SimpleNamespace

    import numpy as np

    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)
    passes = tracker.passes(["ISS"], days=1, min_elevation=0, horizon=0, top=50, start=ISS_EPOCH)
    p = passes[0]
    peak = tracker.ts.from_datetime(datetime.fromisoformat(p.peak))
    calls = []

    class Satellite:
        def at(self, times):
            calls.append(times)
            # Only the initial sample is sunlit, never culmination.
            return SimpleNamespace(is_sunlit=lambda eph: np.arange(len(times.tt)) == 0)

    class Earth:
        def __add__(self, observer):
            return self

        def at(self, times):
            self.times = times
            return self

        def observe(self, sun):
            return self

        def apparent(self):
            return self

        def altaz(self):
            return SimpleNamespace(degrees=np.full(len(self.times.tt), -12.0)), None, None

    tracker._annotate_visible(
        p, Satellite(), peak, tracker._observer(), {"earth": Earth(), "sun": object()}
    )
    assert p.visible is True
    assert len(calls[0].tt) > 2
    assert peak.tt in calls[0].tt


def test_track_includes_exact_end_between_sample_steps(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)
    end = ISS_EPOCH + timedelta(seconds=37)
    track = tracker.track("ISS", ISS_EPOCH, end)
    assert datetime.fromisoformat(track.set.replace("Z", "+00:00")) == end


def test_track_peak_matches_predicted_culmination(tmp_path):
    tracker = SatTracker(_store_with(tmp_path), SAN_FRANCISCO)
    passes = tracker.passes(["ISS"], days=2, min_elevation=20, horizon=10, top=50, start=ISS_EPOCH)
    p = max(passes, key=lambda p: p.max_elevation_deg)
    track = tracker.track("ISS", datetime.fromisoformat(p.rise), datetime.fromisoformat(p.set))
    assert abs(track.max_elevation_deg - p.max_elevation_deg) < 0.01
