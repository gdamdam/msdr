import asyncio
import json
from pathlib import Path

from msdr.providers.nextpass import NextPassProvider, _attach_radio_downlinks, _radio_downlink
from msdr.providers.nextpass import Pass as NextPassPass
from msdr.settings import Location

SAMPLE_PASS = {
    "satellite": "M2-3",
    "norad": 57166,
    "rise": "2026-09-22T06:10:00-04:00",
    "peak": "2026-09-22T06:16:00-04:00",
    "set": "2026-09-22T06:22:00-04:00",
    "max_elevation_deg": 45.2,
    "rise_azimuth_deg": 10.0,
    "peak_azimuth_deg": 90.0,
    "set_azimuth_deg": 170.0,
    "range_at_peak_km": 850.0,
    "window_minutes": 12.0,
    "geometry": "ascending",
    "rank": 1,
}

# Shared query defaults for tests that don't care about the specific values.
_KW = dict(min_elevation=20.0, horizon=10.0, top=50, location=None)


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr

    def kill(self):
        pass

    async def wait(self):
        pass


def _install_fake_subprocess(
    monkeypatch, calls, *, doc=None, returncode=0, stderr=b"", missing=False
):
    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        if missing:
            raise FileNotFoundError(args[0])
        if doc is not None:
            json_path = args[args.index("--json") + 1]
            with open(json_path, "w") as fh:
                json.dump(doc, fh)
        return _FakeProc(returncode, stderr)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


async def test_parses_passes_from_json(monkeypatch):
    calls = []
    _install_fake_subprocess(
        monkeypatch, calls, doc={"timezone": "America/New_York", "passes": [SAMPLE_PASS]}
    )

    provider = NextPassProvider(bin="nextpass")
    result = await provider.passes(["M2-3"], days=2, **_KW)

    assert result.error is None
    assert result.source == "fresh"
    assert result.timezone == "America/New_York"
    assert len(result.passes) == 1
    assert result.passes[0].satellite == "M2-3"
    assert result.passes[0].norad == 57166
    assert len(calls) == 1
    assert "--offline" in calls[0]


async def test_second_call_is_cached_no_subprocess(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, doc={"timezone": "UTC", "passes": [SAMPLE_PASS]})
    provider = NextPassProvider(bin="nextpass", cache_ttl_s=600)

    first = await provider.passes(["M2-3"], days=2, **_KW)
    second = await provider.passes(["M2-3"], days=2, **_KW)

    assert len(calls) == 1
    assert first.source == "fresh"
    assert second.source == "cache"
    assert second.passes == first.passes


async def test_refresh_bypasses_cache_and_repopulates(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, doc={"timezone": "UTC", "passes": [SAMPLE_PASS]})
    provider = NextPassProvider(bin="nextpass")

    await provider.passes(["M2-3"], days=2, **_KW)
    refreshed = await provider.passes(["M2-3"], days=2, refresh=True, **_KW)
    cached_after = await provider.passes(["M2-3"], days=2, **_KW)

    assert len(calls) == 2
    assert "--refresh" in calls[1]
    assert "--offline" not in calls[1]
    assert refreshed.source == "fresh"
    assert cached_after.source == "cache"  # refresh's own result repopulated the cache


async def test_missing_binary_returns_error_and_empty_passes(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, missing=True)
    provider = NextPassProvider(bin="nope-this-does-not-exist")

    result = await provider.passes(["ISS"], days=1, **_KW)

    assert result.passes == []
    assert result.error == "nextpass not installed"


async def test_nonzero_exit_reports_stderr_tail(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, returncode=1, stderr=b"boom: bad satellite label")
    provider = NextPassProvider(bin="nextpass")

    result = await provider.passes(["ISS"], days=1, **_KW)

    assert result.passes == []
    assert "boom: bad satellite label" in result.error


async def test_error_result_is_not_cached(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, returncode=1, stderr=b"transient failure")
    provider = NextPassProvider(bin="nextpass")

    await provider.passes(["ISS"], days=1, **_KW)
    await provider.passes(["ISS"], days=1, **_KW)

    assert len(calls) == 2  # a failed run must not pin the error for the TTL


async def test_argv_contains_days_min_elevation_horizon_top_and_satellites(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, doc={"timezone": "UTC", "passes": []})
    provider = NextPassProvider(bin="nextpass")

    await provider.passes(
        ["ISS", "CSS"], days=3, min_elevation=25.0, horizon=15.0, top=10, location=None
    )

    argv = calls[0]
    assert argv[argv.index("--days") + 1] == "3"
    assert argv[argv.index("--min-elevation") + 1] == "25.0"
    assert argv[argv.index("--horizon") + 1] == "15.0"
    assert argv[argv.index("--top") + 1] == "10"
    assert argv[argv.index("--satellites") + 1] == "ISS,CSS"
    assert "--radio" in argv
    assert "--lat" not in argv


async def test_argv_includes_location_flags_only_when_location_set(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, doc={"timezone": "UTC", "passes": []})
    provider = NextPassProvider(bin="nextpass")
    location = Location(lat=37.7, lon=-122.4, altitude_m=15.0, timezone="America/Los_Angeles")

    await provider.passes(
        ["ISS"], days=2, min_elevation=20.0, horizon=10.0, top=50, location=location
    )

    argv = calls[0]
    assert argv[argv.index("--lat") + 1] == "37.7"
    assert argv[argv.index("--lon") + 1] == "-122.4"
    assert argv[argv.index("--altitude") + 1] == "15.0"
    assert argv[argv.index("--timezone") + 1] == "America/Los_Angeles"


async def test_cache_key_differs_per_parameter(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, doc={"timezone": "UTC", "passes": [SAMPLE_PASS]})
    provider = NextPassProvider(bin="nextpass", cache_ttl_s=600)

    base_kw = dict(days=2, min_elevation=20.0, horizon=10.0, top=50, location=None)
    await provider.passes(["M2-3"], **base_kw)
    await provider.passes(["M2-3"], **{**base_kw, "days": 3})
    await provider.passes(["M2-3"], **{**base_kw, "min_elevation": 30.0})
    await provider.passes(["M2-3"], **{**base_kw, "horizon": 15.0})
    await provider.passes(["M2-3"], **{**base_kw, "top": 10})
    await provider.passes(["M2-3"], **{**base_kw, "location": Location(lat=1.0, lon=2.0)})
    # Re-running the exact original query is still a cache hit.
    await provider.passes(["M2-3"], **base_kw)

    assert len(calls) == 6  # every distinct parameter combination is its own subprocess call


# --- --radio downlink parsing ---------------------------------------------


def test_radio_downlink_picks_first_row_with_a_frequency():
    rows = [
        {"frequency_hz": None, "downlink_low_hz": None, "mode": "unknown"},
        {"frequency_hz": 145825000, "mode": "FM", "description": "APRS digipeater"},
        {"frequency_hz": 437800000, "mode": "FM", "description": "backup"},
    ]
    downlink = _radio_downlink(rows)
    assert downlink.frequency_hz == 145825000
    assert downlink.mode == "FM"
    assert downlink.label == "APRS digipeater"


def test_radio_downlink_falls_back_to_downlink_low_hz_and_default_label():
    rows = [{"downlink_low_hz": 137900000, "downlink_high_hz": 137900000}]
    downlink = _radio_downlink(rows)
    assert downlink.frequency_hz == 137900000
    assert downlink.mode == "unknown"
    assert downlink.label == "unknown downlink"


def test_radio_downlink_none_when_no_usable_row():
    assert _radio_downlink([]) is None
    assert _radio_downlink([{"mode": "FM"}]) is None  # no frequency at all


def test_attach_radio_downlinks_matches_by_norad_string_key():
    passes = [NextPassPass(**{**SAMPLE_PASS, "norad": 25544})]
    radio = {"satellites": {"25544": [{"frequency_hz": 145825000, "mode": "FM"}]}}

    _attach_radio_downlinks(passes, radio)

    assert passes[0].downlink.frequency_hz == 145825000


def test_attach_radio_downlinks_tolerates_unavailable_shape():
    # meteor_passes.py substitutes {"status": "unavailable", "satellites": {}}
    # when SatNOGS metadata couldn't be loaded at all.
    passes = [NextPassPass(**{**SAMPLE_PASS, "norad": 25544})]
    _attach_radio_downlinks(passes, {"status": "unavailable", "satellites": {}})
    assert passes[0].downlink is None
    _attach_radio_downlinks(passes, None)
    assert passes[0].downlink is None


async def test_extra_satellites_write_catalog_json_and_pass_flag(monkeypatch):
    calls = []
    catalog_contents = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        # Read the catalog file here, before _run()'s finally: block deletes
        # the whole tmpdir it lives in.
        catalog_path = Path(args[args.index("--catalog") + 1])
        catalog_contents.append(json.loads(catalog_path.read_text()))
        json_path = args[args.index("--json") + 1]
        with open(json_path, "w") as fh:
            json.dump({"timezone": "UTC", "passes": []}, fh)
        return _FakeProc(0, b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    provider = NextPassProvider(bin="nextpass")

    await provider.passes(
        ["CUSTOM-1"],
        days=2,
        **_KW,
        extra_satellites=[{"label": "CUSTOM-1", "norad": 99999}],
    )

    assert catalog_contents == [
        [{"norad": 99999, "label": "CUSTOM-1", "group": "custom", "name": "CUSTOM-1"}]
    ]


async def test_no_extra_satellites_omits_catalog_flag(monkeypatch):
    calls = []
    _install_fake_subprocess(monkeypatch, calls, doc={"timezone": "UTC", "passes": []})
    provider = NextPassProvider(bin="nextpass")

    await provider.passes(["ISS"], days=2, **_KW)

    assert "--catalog" not in calls[0]


async def test_parses_downlink_from_radio_json(monkeypatch):
    calls = []
    doc = {
        "timezone": "UTC",
        "passes": [SAMPLE_PASS],
        "radio": {"satellites": {"57166": [{"frequency_hz": 137900000, "mode": "LRPT"}]}},
    }
    _install_fake_subprocess(monkeypatch, calls, doc=doc)
    provider = NextPassProvider(bin="nextpass")

    result = await provider.passes(["M2-3"], days=2, **_KW)

    assert result.passes[0].downlink.frequency_hz == 137900000
    assert result.passes[0].downlink.mode == "LRPT"
