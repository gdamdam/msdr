"""ScannerEngine's spectrum source migrated onto msdr.survey.SpectrumHub
(targeted change -- see msdr/engine/scanner.py's `spectrum_hub` kwarg): with
a hub set, the engine acquires/releases a hub queue instead of driving
`set_spectrum_stream` itself, so scanning and Survey can share one stream.
Behaviour must stay identical to the pre-migration direct-adapter path
covered by tests/engine/test_scanner.py.
"""

import asyncio
import time

import pytest

from msdr.engine.confirmed import ConfirmedState
from msdr.engine.dwell import DwellObserver
from msdr.engine.fake_adapter import FakeAdapter
from msdr.engine.scanner import ScanConfig, ScannerEngine, SpectrumSource
from msdr.radio.registry import RESTORABLE
from msdr.storage.db import Database
from msdr.storage.repos import ListRepo, SignalRepo
from msdr.survey.spectrum_hub import SpectrumHub


def _confirmed_from_adapter(adapter: FakeAdapter) -> ConfirmedState:
    cs = ConfirmedState(api_version=str(adapter.state.get("api_version", "")))
    cs.seed({spec.name: adapter.state[spec.name] for spec in RESTORABLE})
    cs.props["signal_snr"] = adapter.state.get("signal_snr")
    cs.props["signal_power"] = adapter.state.get("signal_power")
    return cs


async def _wait_until(cond, timeout: float = 2.0, interval: float = 0.005) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def repos(db):
    return ListRepo(db), SignalRepo(db)


def _engine(adapter, confirmed, lists, signals, *, spectrum_hub=None):
    dwell = DwellObserver(confirmed, dwell_seconds=1.0, on_dwell=lambda *a: None)

    async def _capture(recipe=None):
        raise AssertionError("capture() should not be called in this test")

    return ScannerEngine(
        adapter,
        confirmed,
        lists,
        signals,
        dwell,
        get_nearby_hz=lambda: 0,
        capture=_capture,
        spectrum_hub=spectrum_hub,
    )


async def test_scanner_acquires_hub_on_start_and_releases_on_stop(repos):
    lists, signals = repos
    adapter = FakeAdapter()
    hub = SpectrumHub(adapter)
    await hub.start()
    confirmed = _confirmed_from_adapter(adapter)
    engine = _engine(adapter, confirmed, lists, signals, spectrum_hub=hub)

    config = ScanConfig(source=SpectrumSource(level_threshold=20), dwell_ms=20, settle_ms=10)
    await engine.start(config)
    await _wait_until(lambda: hub.consumers == 1)
    # Enabling the stream is now the hub's job, done exactly once, not the
    # engine calling set_spectrum_stream directly on top of it.
    assert adapter.calls.count(("set_spectrum_stream", True)) == 1

    await engine.stop()
    await _wait_until(lambda: hub.consumers == 0)
    assert adapter.calls.count(("set_spectrum_stream", False)) == 1
    await hub.stop()


async def test_scanner_still_detects_candidates_via_hub(repos):
    lists, signals = repos
    adapter = FakeAdapter()
    target = 145_787_500  # snapped to the 12_500 Hz grid
    adapter.spectrum_peaks = {target: 160}
    hub = SpectrumHub(adapter)
    await hub.start()

    confirmed = _confirmed_from_adapter(adapter)
    confirmed.props["signal_snr"] = 0.0
    engine = _engine(adapter, confirmed, lists, signals, spectrum_hub=hub)

    async def _snr_watcher():
        from msdr.radio.protocol import PropertyChanged

        q = adapter.subscribe()
        try:
            while True:
                ev = await q.get()
                if isinstance(ev, PropertyChanged) and ev.property == "device_vfo_frequency":
                    confirmed.props["signal_snr"] = 20.0 if ev.value == target else 0.0
        finally:
            adapter.unsubscribe(q)

    watcher = asyncio.create_task(_snr_watcher())
    try:
        config = ScanConfig(
            source=SpectrumSource(level_threshold=20),
            dwell_ms=30,
            settle_ms=10,
            threshold_snr_db=10.0,
            hang_s=0.05,
        )
        await engine.start(config)
        await _wait_until(lambda: engine.state == "holding", timeout=2.0)
        assert engine.channel is not None and engine.channel.frequency_hz == target
        await engine.stop()
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    await hub.stop()
