"""Scanner finds log (2026-09-25 plan, Stream A): keep_scanning's
finds_dwell_s hold cutoff, on_find/classify wiring, and skip_ignored. Same
FakeAdapter + real ListRepo/SignalRepo pattern as test_scanner.py.
"""

import asyncio
import time

import pytest

from msdr.engine import scanner as scanner_module
from msdr.engine.confirmed import ConfirmedState
from msdr.engine.dwell import DwellObserver
from msdr.engine.fake_adapter import FakeAdapter
from msdr.engine.scanner import ListSource, ScanConfig, ScannerEngine
from msdr.library.models import Signal
from msdr.radio.registry import RESTORABLE
from msdr.storage.db import Database
from msdr.storage.repos import ListRepo, SignalRepo


def _confirmed_from_adapter(adapter: FakeAdapter) -> ConfirmedState:
    cs = ConfirmedState(api_version=str(adapter.state.get("api_version", "")))
    cs.seed({spec.name: adapter.state[spec.name] for spec in RESTORABLE})
    cs.props["signal_snr"] = adapter.state.get("signal_snr")
    cs.props["signal_power"] = adapter.state.get("signal_power")
    return cs


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def repos(db):
    return ListRepo(db), SignalRepo(db)


async def _wait_until(cond, timeout: float = 2.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


def _engine(adapter, confirmed, lists, signals, *, on_find=None, classify=None, is_ignored=None):
    dwell = DwellObserver(confirmed, dwell_seconds=1.0, on_dwell=lambda *a: None)

    async def _capture(recipe=None):
        raise AssertionError("capture() should not be called in these tests")

    engine = ScannerEngine(
        adapter,
        confirmed,
        lists,
        signals,
        dwell,
        get_nearby_hz=lambda: 0,
        capture=_capture,
        on_find=on_find,
        classify=classify,
        is_ignored=is_ignored,
    )
    return engine


async def test_keep_scanning_ends_hold_at_finds_dwell_s_while_snr_stays_high(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    signals.add(a)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)  # seeds signal_snr=12.0, never dropped below
    engine = _engine(adapter, confirmed, lists, signals)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=5.0,  # would never fire on its own -- SNR stays high throughout
        keep_scanning=True,
        finds_dwell_s=0.05,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")

    started = time.monotonic()
    await _wait_until(lambda: engine.state != "holding", timeout=2.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0  # ended near finds_dwell_s, nowhere near hang_s
    assert engine.hits[0].duration_s >= 0.05
    await engine.stop()


async def test_on_find_called_once_with_duration_demod_bandwidth_and_classification(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    signals.add(a)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    finds: list[dict] = []

    async def on_find(payload):
        finds.append(payload)

    async def classify(frequency_hz):
        return {"source": "identify", "bw_class": "narrow"}

    engine = _engine(adapter, confirmed, lists, signals, on_find=on_find, classify=classify)
    config = ScanConfig(
        source=ListSource(list_id=lst["id"]),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.03,
        log_finds=True,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")
    confirmed.props["signal_snr"] = 0.0  # let the hang timeout end the hold
    await _wait_until(lambda: len(finds) == 1, timeout=2.0)

    payload = finds[0]
    assert payload["frequency_hz"] == 146_000_000
    assert payload["duration_s"] > 0
    assert payload["demodulator"] == "NFM"
    assert payload["bandwidth_hz"] == 12_500
    assert payload["classification"] == {"source": "identify", "bw_class": "narrow"}
    await engine.stop()


async def test_classify_timeout_logs_none_but_on_find_still_called(repos, monkeypatch):
    monkeypatch.setattr(scanner_module, "_CLASSIFY_TIMEOUT_S", 0.05)

    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    signals.add(a)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    finds: list[dict] = []

    async def on_find(payload):
        finds.append(payload)

    async def classify(frequency_hz):
        await asyncio.sleep(1.0)  # far longer than the patched 0.05s timeout
        return {"never": "seen"}

    engine = _engine(adapter, confirmed, lists, signals, on_find=on_find, classify=classify)
    config = ScanConfig(
        source=ListSource(list_id=lst["id"]),
        dwell_ms=20,
        settle_ms=10,
        threshold_snr_db=10.0,
        hang_s=0.03,
        log_finds=True,
    )
    await engine.start(config)
    await _wait_until(lambda: engine.state == "holding")
    confirmed.props["signal_snr"] = 0.0
    await _wait_until(lambda: len(finds) == 1, timeout=2.0)

    assert finds[0]["classification"] is None
    await engine.stop()


async def test_ignored_channel_skipped_and_counted(repos):
    lists, signals = repos
    a = Signal(name="A", frequency_hz=146_000_000)
    b = Signal(name="B", frequency_hz=146_100_000)
    signals.add(a)
    signals.add(b)
    lst = lists.create("Sweep")
    lists.add_items(lst["id"], [a.id, b.id])

    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0  # stay in "running" -- no holds to confuse things
    engine = _engine(adapter, confirmed, lists, signals, is_ignored=lambda hz: hz == a.frequency_hz)

    config = ScanConfig(
        source=ListSource(list_id=lst["id"]), dwell_ms=1000, settle_ms=10, threshold_snr_db=10.0
    )
    await engine.start(config)

    # A is ignored, so the scan must land straight on B without ever tuning A.
    await _wait_until(
        lambda: engine.channel is not None and engine.channel.signal_id == b.id, timeout=1.0
    )
    assert engine.skipped_ignored >= 1
    assert engine.status()["skipped_ignored"] == engine.skipped_ignored
    await engine.stop()


def test_scan_config_defaults_preserve_todays_behaviour():
    config = ScanConfig(source=ListSource(list_id="x"), dwell_ms=10)
    assert config.log_finds is False
    assert config.keep_scanning is False
    assert config.finds_dwell_s == 3.0
    assert config.skip_ignored is True
