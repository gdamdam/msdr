"""Decoder/scanner lifecycle robustness (S5 plan §2):

(a) cancelling DecoderManager.start_decoding before its backend reports
    running leaves no leaked manager state (channel/pump/event tasks).
(b) ReplayDecodeSession.start_single on a receiver whose demodulator isn't
    in a decoder's spec reports session_state == "unsupported_input" and
    never starts a backend.
(c) a ReplaySource seek followed immediately by ReplayDecodeSession.stop()
    while the tap pump is actively feeding blocks stops cleanly, no
    exception.
(d) all scan channels ignored: asserts ScannerEngine's *existing* (0.24.0)
    status-reporting behaviour rather than adding new engine logic -- see
    that test's own comment for the gap this leaves.
"""

import asyncio
import contextlib
import json
from pathlib import Path

import numpy as np
import pytest

from msdr.decoder.backends import DecodeEvent
from msdr.decoder.manager import DecoderManager
from msdr.decoder.replay_decode import ReplayDecodeSession
from msdr.engine.confirmed import ConfirmedState
from msdr.engine.dwell import DwellObserver
from msdr.engine.fake_adapter import FakeAdapter
from msdr.engine.scanner import ListSource, ScanConfig, ScannerEngine
from msdr.library.models import Signal
from msdr.radio.registry import RESTORABLE
from msdr.settings import DecoderSettings
from msdr.source.virtual_receiver import VirtualReceiver
from msdr.storage.db import Database
from msdr.storage.repos import ListRepo, SignalRepo
from msdr.survey.replay import ReplaySource

# --- (a) cancel decoder startup: no leaked process/task ---------------------


class _SlowBackend:
    """build_backend() double whose start() blocks on a shared gate, so a
    test can cancel the manager's start_decoding() task while it's still
    inside `await backend.start()` -- i.e. before the backend ever reports
    "running"."""

    def __init__(self, decoder_id: str, gate: asyncio.Event):
        self.decoder_id = decoder_id
        self._gate = gate
        self.entered_start = False
        self.completed_start = False

    async def start(self) -> None:
        self.entered_start = True
        await self._gate.wait()
        self.completed_start = True

    def feed(self, pcm_s16: bytes) -> None:
        pass

    def events(self):
        async def _gen():
            return
            yield  # pragma: no cover - makes this an async generator

        return _gen()

    async def stop(self) -> None:
        pass

    @property
    def error(self) -> str | None:
        return None


async def test_cancel_decoder_startup_leaves_no_leaked_state(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    gate = asyncio.Event()
    backends: dict[str, _SlowBackend] = {}

    def fake_build_backend(spec, binaries, calls_dir=None, fax_dir=None):
        backend = _SlowBackend(spec.id, gate)
        backends[spec.id] = backend
        return backend

    monkeypatch.setattr("msdr.decoder.manager.build_backend", fake_build_backend)
    monkeypatch.setattr(
        "msdr.decoder.manager.resolve_binaries",
        lambda bin_dirs: {"multimon-ng": "/fake/multimon-ng"},
    )
    monkeypatch.setattr("msdr.decoder.manager.available", lambda bin_dirs: {})

    manager = DecoderManager(
        adapter=adapter,
        stream_factory=lambda: stream,
        repo=repo,
        publish=published.append,
        settings_getter=lambda: settings,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
    )

    task = asyncio.create_task(manager.start_decoding(["pocsag"], 152_840_000, "manual"))
    await asyncio.sleep(0)  # run the task up to the blocked backend.start()
    assert backends["pocsag"].entered_start
    assert not backends["pocsag"].completed_start

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancelled attempt never got far enough to mark itself active or
    # register any channel/pump/event task -- nothing here needs cleaning up.
    assert manager._active is False
    assert manager._channels == {}
    assert manager._pump_task is None
    assert manager._event_tasks == []

    # Manager state wasn't corrupted by the cancellation: a fresh start still
    # works cleanly.
    gate.set()
    status = await manager.start_decoding(["pocsag"], 152_840_000, "manual")
    assert status["active"] is True
    await manager.stop_decoding()


# --- (b) incompatible input: unsupported_input, no backend started ---------


class _FakeReceiver:
    def __init__(self, demodulator: str):
        self._demodulator = demodulator

    def status(self) -> dict:
        return {"demodulator": self._demodulator}


class _FakeSource:
    source_id = "replay:fake"


async def test_replay_decode_reports_unsupported_input_without_starting_backend():
    # "ais" needs NFM (registry.py) and has no external binary (backend
    # =\"python\", binaries=()), so this exercises the demodulator mismatch
    # alone, not a missing-binary skip.
    session = ReplayDecodeSession(
        _FakeReceiver("WFM"),
        _FakeSource(),
        binaries={},
        settings=DecoderSettings(),
        on_event=lambda e: None,
        on_status=lambda s: None,
    )
    try:
        status = await session.start_single(["ais"])
        (ais_status,) = status["decoders"]
        assert ais_status["session_state"] == "unsupported_input"
        assert "NFM" in ais_status["unsupported"] and "WFM" in ais_status["unsupported"]
        assert session._singles["ais"].backend is None
        assert status["mode"] == "single"
    finally:
        await session.stop()


# --- (c) seek + stop while the pump is mid-block: clean, no exception ------


def _write_lifecycle_recording(root: Path) -> tuple[Path, int]:
    fs = 2_000_000
    center_hz = 462_562_500
    seconds = 2.0
    n = int(seconds * fs)
    rng = np.random.default_rng(7)
    noise = (0.02 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
    iq16 = np.empty(2 * n, dtype="<i2")
    iq16[0::2] = np.clip(noise.real * 32767, -32768, 32767).astype("<i2")
    iq16[1::2] = np.clip(noise.imag * 32767, -32768, 32767).astype("<i2")
    d = root / "rec"
    d.mkdir()
    stem = f"run-01_{fs}SPS_{center_hz}Hz"
    (d / f"{stem}.sigmf-data").write_bytes(iq16.tobytes())
    meta = {
        "global": {"core:datatype": "ci16_le", "core:sample_rate": fs, "core:version": "1.0.0"},
        "captures": [{"core:sample_start": 0, "core:frequency": center_hz}],
        "annotations": [],
    }
    (d / f"{stem}.sigmf-meta").write_text(json.dumps(meta))
    index = {
        "start_wall": 1_700_000_000.0,
        "end_wall": 1_700_000_000.0 + seconds,
        "preroll_s": 0,
        "runs": [
            {
                "data": f"{stem}.sigmf-data",
                "meta": f"{stem}.sigmf-meta",
                "sample_rate": fs,
                "center_hz": center_hz,
                "samples": n,
                "start_wall": 1_700_000_000.0,
                "settings": {},
            }
        ],
    }
    (d / "recording.json").write_text(json.dumps(index))
    return d, center_hz


async def test_seek_then_stop_while_pump_is_active_is_clean(tmp_path):
    rec, center_hz = _write_lifecycle_recording(tmp_path)
    source = ReplaySource(rec, loop=True)
    rx = VirtualReceiver(source)
    rx.tune(center_hz, "NFM", 12_500)
    await rx.start()
    session = ReplayDecodeSession(
        rx,
        source,
        binaries={},
        settings=DecoderSettings(),
        on_event=lambda e: None,
        on_status=lambda s: None,
        calls_dir=tmp_path / "calls",
        fax_dir=tmp_path / "fax",
    )
    try:
        await session.start_single(["ais"])
        await asyncio.sleep(0.05)  # let the tap pump subscribe before playback
        await source.start()
        await asyncio.sleep(0.2)  # pump is actively feeding blocks
        source.seek(0.0)  # generation bump queued; the pump applies it mid-stream
        await session.stop("test")  # must not raise even with a restart pending
    finally:
        with contextlib.suppress(Exception):
            await session.stop()
        await source.stop()
        await rx.stop()

    status = session.status()
    assert status["active"] is False
    assert isinstance(status["restarts"], int)
    assert status["restarts"] >= 0


# --- (e) stale queued backend result dropped after a seek, before restart --


class _GenReceiver:
    """Minimal receiver double: only what the single-mode path reads off
    status() (demodulator, generation, channel_generation). generation is
    mutated directly to simulate a seek landing before the tap pump has fed
    a new-generation block (and so before _restart_tap runs)."""

    def __init__(self) -> None:
        self.generation = 0
        self.channel_generation = 0
        self.dropped_tap_blocks = 0

    def status(self) -> dict:
        return {
            "demodulator": "NFM",
            "generation": self.generation,
            "channel_generation": self.channel_generation,
        }

    async def flat_tap(self):
        # Never yields: keeps the tap pump parked so the test drives
        # _feed_block itself instead of racing a real one.
        await asyncio.Future()
        yield  # pragma: no cover - unreachable, keeps this an async generator


class _QueuedBackend:
    """build_backend() double whose events() replays whatever is queued,
    like an external decoder process reporting asynchronously."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.error: str | None = None
        self.stopped = False

    async def start(self) -> None:
        pass

    def feed(self, pcm_s16: bytes) -> None:
        pass

    async def events(self):
        while True:
            yield await self.queue.get()

    async def stop(self) -> None:
        self.stopped = True


async def test_replay_tap_gap_restarts_single_backend_and_framing(monkeypatch):
    backends: list[_QueuedBackend] = []

    def fake_build_backend(spec, binaries, calls_dir=None, fax_dir=None):
        backend = _QueuedBackend()
        backends.append(backend)
        return backend

    monkeypatch.setattr("msdr.decoder.replay_decode.build_backend", fake_build_backend)
    receiver = _GenReceiver()
    session = ReplayDecodeSession(
        receiver,
        _FakeSource(),
        binaries={},
        settings=DecoderSettings(),
        on_event=lambda e: None,
        on_status=lambda s: None,
    )
    try:
        await session.start_single(["ais"])
        pcm = np.zeros(160, dtype="<i2").tobytes()
        await session._feed_block("flat", pcm, 0, 0)
        old_backend = backends[0]

        # VirtualReceiver bumps channel_generation on an IQ sample-index gap;
        # the next tap block must recreate protocol framing and DSP state.
        receiver.channel_generation += 1
        changed = await session._feed_block("flat", pcm, 0, 160)

        assert changed
        assert session.restarts == 1
        assert len(backends) == 2
        assert old_backend.stopped
        assert session._singles["ais"].backend is backends[1]
        assert session._singles["ais"].start_generation == (0, 1)
    finally:
        await session.stop()


async def test_replay_first_tap_block_after_seek_restarts_backend(monkeypatch):
    backends: list[_QueuedBackend] = []

    def fake_build_backend(spec, binaries, calls_dir=None, fax_dir=None):
        backend = _QueuedBackend()
        backends.append(backend)
        return backend

    monkeypatch.setattr("msdr.decoder.replay_decode.build_backend", fake_build_backend)
    receiver = _GenReceiver()
    session = ReplayDecodeSession(
        receiver,
        _FakeSource(),
        binaries={},
        settings=DecoderSettings(),
        on_event=lambda e: None,
        on_status=lambda s: None,
    )
    try:
        await session.start_single(["ais"])
        old_backend = backends[0]

        # Seek before the pump has delivered even one tap block. There is no
        # last-fed generation to compare, but the backend still belongs to 0.
        receiver.generation = 1
        receiver.channel_generation = 1
        changed = await session._feed_block("flat", np.zeros(160, dtype="<i2").tobytes(), 1, 0)

        assert changed
        assert session.restarts == 1
        assert len(backends) == 2
        assert old_backend.stopped
        assert session._singles["ais"].backend is backends[1]
        assert session._singles["ais"].start_generation == (1, 1)
    finally:
        await session.stop()


async def test_stale_backend_result_dropped_after_seek_before_restart(monkeypatch):
    backend = _QueuedBackend()
    monkeypatch.setattr(
        "msdr.decoder.replay_decode.build_backend",
        lambda spec, binaries, calls_dir=None, fax_dir=None: backend,
    )
    receiver = _GenReceiver()
    events: list = []
    session = ReplayDecodeSession(
        receiver,
        _FakeSource(),
        binaries={},
        settings=DecoderSettings(),
        on_event=events.append,
        on_status=lambda s: None,
    )
    try:
        await session.start_single(["ais"])  # backend started for generation 0
        pcm = np.zeros(160, dtype="<i2").tobytes()
        await session._feed_block("flat", pcm, 0, 0)  # last block fed under generation 0

        receiver.generation = 1  # a seek: source moved on; no new block fed yet,
        # so _restart_tap hasn't run and the generation-0 backend is still alive.

        await backend.queue.put(
            DecodeEvent(ts=0.0, decoder="ais", kind="text", text="stale", fields={})
        )
        for _ in range(5):
            await asyncio.sleep(0)  # let _drain consume the queued event

        assert events == []  # generation-0 result dropped, not emitted as current
    finally:
        await session.stop()


# --- (d) all scan channels ignored: assert existing status behaviour -------


def _confirmed_from_adapter(adapter: FakeAdapter) -> ConfirmedState:
    cs = ConfirmedState(api_version=str(adapter.state.get("api_version", "")))
    cs.seed({spec.name: adapter.state[spec.name] for spec in RESTORABLE})
    cs.props["signal_snr"] = adapter.state.get("signal_snr")
    cs.props["signal_power"] = adapter.state.get("signal_power")
    return cs


async def test_all_channels_ignored_status_has_no_dedicated_reason_text():
    db = Database(":memory:")
    db.connect()
    try:
        lists, signals = ListRepo(db), SignalRepo(db)
        a = Signal(name="A", frequency_hz=146_000_000)
        b = Signal(name="B", frequency_hz=146_100_000)
        signals.add(a)
        signals.add(b)
        lst = lists.create("Ignored")
        lists.add_items(lst["id"], [a.id, b.id])

        adapter = FakeAdapter()
        confirmed = _confirmed_from_adapter(adapter)
        dwell = DwellObserver(confirmed, dwell_seconds=1.0, on_dwell=lambda *a: None)

        async def _capture(recipe=None):
            raise AssertionError("capture() should not be called in this test")

        async def _on_status(status):
            pass

        async def _on_hit(hit):
            pass

        engine = ScannerEngine(
            adapter,
            confirmed,
            lists,
            signals,
            dwell,
            get_nearby_hz=lambda: 0,
            capture=_capture,
            on_status=_on_status,
            on_hit=_on_hit,
        )
        engine._is_ignored = lambda _hz: True
        config = ScanConfig(
            source=ListSource(list_id=lst["id"]),
            dwell_ms=1000,
            settle_ms=10,
            threshold_snr_db=10.0,
        )
        await engine.start(config)
        await asyncio.sleep(0.2)

        status = engine.status()
        # Existing 0.24.0 behaviour (msdr/engine/scanner.py _visit_channel /
        # _run): the ignored-channel branch never tunes and never sets
        # self.reason (that field is only ever paired with state="paused",
        # set by pause() -- see scanner.py). "All channels ignored" is
        # visible today only as skipped_ignored reaching the channel count
        # while `channel` stays null and `state` stays "running" -- there is
        # no dedicated status text saying so. Asserting that existing signal
        # here rather than adding a new `reason` string to scanner.py: doing
        # so would mean overloading a field whose only current meaning is
        # "why is the scan paused", which this path is not -- a genuine
        # design choice for whoever owns scanner.py, not a mechanical
        # addition, so it's flagged in the S5 report instead of picked here.
        assert status["skipped_ignored"] >= 2
        assert status["channel"] is None
        assert status["reason"] is None
        assert status["state"] == "running"
        # S5: the visible outcome -- a whole pass tuned nothing.
        assert status["all_ignored"] is True

        await asyncio.wait_for(engine.stop(), 2.0)
        assert engine.state == "idle"
    finally:
        db.close()
