import asyncio
from dataclasses import dataclass, field

import pytest

from msdr.survey.beacons import (
    NCDXF_BANDS_HZ,
    BeaconError,
    BeaconMonitor,
    beacon_on,
    schedule,
    slot_at,
)

# -- schedule math --------------------------------------------------------


def test_slot_at_slot0():
    idx, start, end = slot_at(0.0)
    assert (idx, start, end) == (0, 0.0, 10.0)


def test_slot0_4u1un_on_14100():
    assert beacon_on(14_100_000, 0.0) == "4U1UN"
    assert schedule(0.0)[0] == (14_100_000, "4U1UN")


def test_slot1_ve8at_on_14100_and_4u1un_on_18110():
    t = 10.0  # slot 1 starts at t=10
    idx, _, _ = slot_at(t)
    assert idx == 1
    assert beacon_on(14_100_000, t) == "VE8AT"
    assert beacon_on(18_110_000, t) == "4U1UN"


def test_schedule_wraps_after_18_slots():
    # 18 slots * 10s = 180s -- slot 18 is slot 0 again.
    idx, start, end = slot_at(180.0)
    assert (idx, start, end) == (0, 180.0, 190.0)
    assert beacon_on(14_100_000, 180.0) == "4U1UN"


def test_beacon_on_unknown_band_is_none():
    assert beacon_on(1_000_000, 0.0) is None


# -- session fakes ----------------------------------------------------------


class FakeAdapter:
    def __init__(self, snr: float | None = None):
        self.snr = snr

    async def get(self, prop: str):
        if prop == "signal_snr":
            return self.snr
        return None


class Recorder:
    def __init__(self):
        self.tune_calls: list[tuple[int, str, int]] = []
        self.decode_start_calls: list[int] = []
        self.decode_stop_calls = 0

    async def tune(self, frequency_hz, demodulator, bandwidth_hz):
        self.tune_calls.append((frequency_hz, demodulator, bandwidth_hz))

    async def decode_start(self, frequency_hz):
        self.decode_start_calls.append(frequency_hz)

    async def decode_stop(self):
        self.decode_stop_calls += 1


class FakeRepo:
    """BeaconHitRepo double (Stream B builds the real one): add() stamps an
    id, summary() aggregates per beacon x band the way the real repo's SQL
    query would."""

    def __init__(self):
        self.hits: list[dict] = []

    def add(self, hit: dict) -> dict:
        stored = {**hit, "id": f"h{len(self.hits)}"}
        self.hits.append(stored)
        return stored

    def summary(self) -> list[dict]:
        agg: dict[tuple[str, int], dict] = {}
        for hit in self.hits:
            key = (hit["beacon"], hit["frequency_hz"])
            entry = agg.setdefault(
                key,
                {
                    "beacon": hit["beacon"],
                    "band_hz": hit["frequency_hz"],
                    "last_heard": None,
                    "last_snr_db": None,
                    "decoded": False,
                    "hits": 0,
                },
            )
            entry["hits"] += 1
            entry["last_heard"] = hit["ts"]
            entry["last_snr_db"] = hit["snr_db"]
            entry["decoded"] = entry["decoded"] or hit["decoded"]
        return list(agg.values())


@dataclass
class FakeBeaconSettings:
    snr_threshold_db: float = 6.0
    ndb_frequencies_hz: list = field(default_factory=list)
    ndb_dwell_s: int = 30
    band_order_hz: list = field(default_factory=lambda: list(NCDXF_BANDS_HZ))


class FakeClock:
    """Paired clock+sleep: sleep advances the clock by the requested amount
    instead of waiting real time, so an 18-slot (3-minute) rotation test
    runs instantly -- only asyncio.sleep(0) is used, to actually yield to
    the event loop so the monitor's background task interleaves with the
    test coroutine driving it."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt
        await asyncio.sleep(0)


def make_monitor(*, settings=None, adapter=None, feature_enabled=True):
    clock = FakeClock()
    recorder = Recorder()
    repo = FakeRepo()
    published: list[dict] = []
    adapter = adapter if adapter is not None else FakeAdapter()
    settings = settings if settings is not None else FakeBeaconSettings()
    monitor = BeaconMonitor(
        adapter=adapter,
        tune=recorder.tune,
        decode_start=recorder.decode_start,
        decode_stop=recorder.decode_stop,
        repo=repo,
        publish=published.append,
        settings_getter=lambda: settings,
        feature_enabled=lambda: feature_enabled,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return monitor, repo, published, clock, recorder, settings


async def _pump_until(cond, limit=5000):
    for _ in range(limit):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met within pump limit")


# -- session behaviour --------------------------------------------------------


async def test_feature_disabled_raises_beacon_error():
    monitor, *_ = make_monitor(feature_enabled=False)
    with pytest.raises(BeaconError):
        await monitor.start_session("ncdxf")


async def test_hit_on_snr_is_probable():
    settings = FakeBeaconSettings(snr_threshold_db=6.0, band_order_hz=[14_100_000])
    monitor, repo, _published, _clock, recorder, _settings = make_monitor(
        settings=settings, adapter=FakeAdapter(snr=12.0)
    )
    await monitor.start_session("ncdxf")
    await _pump_until(lambda: len(repo.hits) >= 1)
    await monitor.stop_session()

    hit = repo.hits[0]
    assert hit["beacon"] == "4U1UN"  # slot 0 on 14.100
    assert hit["frequency_hz"] == 14_100_000
    assert hit["probable"] is True
    assert hit["decoded"] is False
    assert recorder.tune_calls[0] == (14_100_000, "cw", 500)


async def test_hit_on_decode_is_decoded_not_probable():
    settings = FakeBeaconSettings(snr_threshold_db=6.0, band_order_hz=[14_100_000])
    monitor, repo, _published, _clock, _recorder, _settings = make_monitor(
        settings=settings,
        adapter=FakeAdapter(snr=1.0),  # below threshold
    )
    await monitor.start_session("ncdxf")
    # Let the first slot start (pending_events reset) before posting the event.
    await _pump_until(lambda: monitor.status()["slot"] == 0)
    monitor.note_decode_event({"text": "DE 4U1UN 4U1UN K", "frequency_hz": 14_100_000})
    await _pump_until(lambda: len(repo.hits) >= 1)
    await monitor.stop_session()

    hit = repo.hits[0]
    assert hit["beacon"] == "4U1UN"
    assert hit["decoded"] is True
    assert hit["probable"] is False


async def test_no_hit_below_threshold_counts_a_miss():
    settings = FakeBeaconSettings(snr_threshold_db=6.0, band_order_hz=[14_100_000])
    monitor, repo, _published, _clock, _recorder, _settings = make_monitor(
        settings=settings, adapter=FakeAdapter(snr=1.0)
    )
    await monitor.start_session("ncdxf")
    await _pump_until(lambda: monitor.status()["slot"] == 1)  # slot 0 has ended
    await monitor.stop_session()

    assert repo.hits == []
    assert monitor.status()["misses"] == {"4U1UN@14100000": 1}


async def test_band_rotates_after_18_slots():
    settings = FakeBeaconSettings(snr_threshold_db=6.0, band_order_hz=[14_100_000, 18_110_000])
    monitor, _repo, _published, _clock, _recorder, _settings = make_monitor(
        settings=settings, adapter=FakeAdapter(snr=1.0)
    )
    await monitor.start_session("ncdxf")
    await _pump_until(lambda: monitor.status()["band_hz"] == 14_100_000)
    await _pump_until(lambda: monitor.status()["band_hz"] == 18_110_000)
    await monitor.stop_session()

    assert monitor.status()["cycle"] >= 1


async def test_ndb_ident_hit():
    monitor, repo, _published, _clock, recorder, _settings = make_monitor(
        adapter=FakeAdapter(snr=None)
    )
    await monitor.start_session("ndb", frequencies_hz=[400_000])
    await _pump_until(lambda: monitor.status()["band_hz"] == 400_000)
    monitor.note_decode_event({"text": "OQ", "frequency_hz": 400_000})
    monitor.note_decode_event({"text": "OQ", "frequency_hz": 400_000})
    await _pump_until(lambda: len(repo.hits) >= 1)
    await monitor.stop_session()

    hit = repo.hits[0]
    assert hit["beacon"] == "OQ"
    assert hit["decoded"] is True
    assert hit["probable"] is False
    assert hit["mode"] == "ndb"
    assert recorder.tune_calls[0] == (400_000, "cw", 500)


async def test_ndb_requires_frequencies():
    monitor, *_ = make_monitor()
    with pytest.raises(BeaconError):
        await monitor.start_session("ndb")


def test_status_shape_when_idle():
    monitor, _repo, _published, _clock, _recorder, _settings = make_monitor()
    status = monitor.status()
    assert status == {
        "active": False,
        "mode": None,
        "band_hz": None,
        "slot": None,
        "current": None,
        "since": None,
        "last_stop_reason": None,
        "cycle": 0,
        "summary": [],
        "misses": {},
    }


def test_publish_cadence_is_throttled_to_1hz():
    monitor, _repo, published, clock, _recorder, _settings = make_monitor()
    monitor._publish_status(force=True)
    n = len(published)

    monitor._publish_status()  # same instant -- suppressed
    assert len(published) == n

    clock.t += 1.5
    monitor._publish_status()  # >=1s later -- goes through
    assert len(published) == n + 1
    assert published[-1]["type"] == "beacons"
