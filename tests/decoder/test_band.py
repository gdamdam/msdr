import asyncio
import contextlib
import subprocess
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.band import BandDecoder, _QueuedIQFrame


def test_source_dsp_can_import_before_decoder_band():
    # A fixed-channel extractor is imported on use so startup does not cycle
    # through source.dsp -> survey -> decoder.band -> source.dsp.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from msdr.source.dsp import ChannelExtractor; "
            "from msdr.decoder.band import BandDecoder",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class FakeIQStream:
    """IQStream double: no socket, just a queue the test feeds directly."""

    def __init__(self):
        self.started = False
        self.enabled = False
        self.connected = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        self.started = True
        self.connected = True

    async def stop(self) -> None:
        self.started = False
        self.connected = False

    async def enable(self, on: bool) -> None:
        self.enabled = on

    async def frames(self):
        while True:
            yield await self._queue.get()

    def push(self, data: bytes, received_at: float | None = None) -> None:
        class _Frame:
            pass

        frame = _Frame()
        frame.data = data
        frame.received_at = received_at if received_at is not None else time.time()
        self._queue.put_nowait(frame)


class FakeBandBackend:
    """build_backend() double for band.py -- mirrors test_manager's FakeBackend."""

    def __init__(self, decoder_id: str):
        self.decoder_id = decoder_id
        self.fed: list[bytes] = []
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()
        self.stopped = False

    async def start(self) -> None:
        pass

    def feed(self, pcm_s16: bytes) -> None:
        self.fed.append(pcm_s16)

    def events(self):
        async def _gen():
            while True:
                yield await self._events.get()

        return _gen()

    async def stop(self) -> None:
        self.stopped = True

    def push(self, event: DecodeEvent) -> None:
        self._events.put_nowait(event)


def _fake_build_backend(unavailable=frozenset()):
    backends: dict[str, FakeBandBackend] = {}

    def _build(spec, binaries, calls_dir=None, fax_dir=None):
        if spec.id in unavailable:
            raise DecoderError(f"{spec.id}: missing binary (fake)")
        backend = FakeBandBackend(spec.id)
        backends[spec.id] = backend
        return backend

    return _build, backends


def _iq_frame_bytes(
    tones_hz: list[float], fs: int, n_samples: int, noise_std: float = 0.01, seed: int = 0
) -> bytes:
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples) / fs
    x = rng.normal(0, noise_std, n_samples) + 1j * rng.normal(0, noise_std, n_samples)
    for freq_hz in tones_hz:
        x = x + np.exp(1j * 2 * np.pi * freq_hz * t)
    x = np.clip(x.real, -1, 1) + 1j * np.clip(x.imag, -1, 1)
    iq = np.empty(n_samples * 2, dtype="<i2")
    iq[0::2] = (x.real * 32767).astype("<i2")
    iq[1::2] = (x.imag * 32767).astype("<i2")
    return iq.tobytes()


FS = 200_000
CENTER_HZ = 150_000_000
SPACING_HZ = 12500
FRAME_SAMPLES = int(FS * 0.05)


async def _wait_until(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _make_band(
    monkeypatch,
    *,
    memories,
    idle_s=60.0,
    max_channels=8,
    decoders=("pocsag",),
    threshold_db=10.0,
    detect=True,
    source_tag="band",
    track_generation=False,
):
    fake_build, backends = _fake_build_backend()
    monkeypatch.setattr("msdr.decoder.band.build_backend", fake_build)

    stream = FakeIQStream()
    events: list[DecodeEvent] = []
    statuses: list[int] = []

    band = BandDecoder(
        iq_stream_factory=lambda: stream,
        decoders=list(decoders),
        binaries={},
        center_hz=CENTER_HZ,
        sample_rate_hz=FS,
        memories=memories,
        max_channels=max_channels,
        threshold_db=threshold_db,
        idle_s=idle_s,
        spacing_hz=SPACING_HZ,
        on_event=events.append,
        on_status=lambda: statuses.append(1),
        detect=detect,
        source_tag=source_tag,
        track_generation=track_generation,
    )
    return band, stream, backends, events, statuses


@pytest.mark.asyncio
async def test_memory_channel_present_from_start(monkeypatch):
    memory_freq = CENTER_HZ - 12_500  # one bin below centre
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[(memory_freq, "sig-1", [])]
    )
    await band.start()
    try:
        status = band.status()
        assert len(status["channels"]) == 1
        channel = status["channels"][0]
        assert channel["source"] == "memory"
        assert channel["signal_id"] == "sig-1"
        assert channel["frequency_hz"] == memory_freq
        assert channel["decoders"] == ["pocsag"]
        assert "pocsag" in backends
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_memory_for_other_protocol_does_not_use_band_slot(monkeypatch):
    frequency_hz = CENTER_HZ - 12_500
    band, _stream, backends, _events, _statuses = _make_band(
        monkeypatch,
        memories=[(frequency_hz, "dmr-memory", ["dmr"])],
        decoders=("pocsag",),
        max_channels=1,
    )
    await band.start()
    try:
        assert band.status()["channels"] == []
        assert backends == {}
        await band.pin(frequency_hz, "dmr-memory", ["dmr"])
        assert band.status()["channels"] == []
    finally:
        await band.stop()


def test_fixed_channel_preserves_off_bin_sideband():
    # At 6 MS/s the nearest 12.5 kHz PFB bin for a signal 10 kHz below
    # center is 2.5 kHz away. Its upper FSK sideband then falls outside the
    # bin's flat passband; the fixed decoder must extract at the exact VFO.
    center_hz = 152_850_000
    frequency_hz = 152_840_000
    fs = 6_000_000
    band = BandDecoder(
        iq_stream_factory=lambda: None,
        decoders=["pocsag"],
        binaries={},
        center_hz=center_hz,
        sample_rate_hz=fs,
        memories=[(frequency_hz, "pager", ["pocsag"])],
        max_channels=0,
        threshold_db=10,
        idle_s=60,
        spacing_hz=12_500,
        on_event=lambda event: None,
        on_status=lambda: None,
        detect=False,
    )
    assert band._fixed_extractor is not None
    # An upper sideband 8 kHz from the VFO is still inside the dedicated
    # 9 kHz passband, even though it is 10.5 kHz from the nearest PFB bin.
    t = np.arange(fs // 20, dtype=np.float64) / fs
    iq = np.exp(2j * np.pi * (-10_000 + 8_000) * t).astype(np.complex64)
    output = band._fixed_extractor.process(iq)
    assert np.median(np.abs(output[-1000:])) > 0.5
    observed_hz = np.median(np.angle(output[1:] * np.conj(output[:-1]))) * 48_000 / (2 * np.pi)
    assert observed_hz == pytest.approx(8_000, abs=30)


@pytest.mark.asyncio
async def test_detected_channel_appears(monkeypatch):
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[], threshold_db=25.0
    )
    await band.start()
    try:
        tone_offset = 2 * SPACING_HZ
        for seed in range(6):
            stream.push(
                _iq_frame_bytes([tone_offset], FS, FRAME_SAMPLES, noise_std=0.02, seed=seed)
            )

        def has_detected():
            return any(c["source"] == "detected" for c in band.status()["channels"])

        assert await _wait_until(has_detected)
        detected = next(c for c in band.status()["channels"] if c["source"] == "detected")
        assert abs(detected["frequency_hz"] - (CENTER_HZ + tone_offset)) < SPACING_HZ / 2
        assert detected["snr_db"] > 10.0
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_idle_channel_is_retired(monkeypatch):
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[], idle_s=0.05, threshold_db=25.0
    )
    await band.start()
    try:
        tone_offset = 3 * SPACING_HZ
        for seed in range(6):
            stream.push(
                _iq_frame_bytes([tone_offset], FS, FRAME_SAMPLES, noise_std=0.02, seed=seed)
            )

        def has_detected():
            return any(c["source"] == "detected" for c in band.status()["channels"])

        assert await _wait_until(has_detected)

        # Idle retirement is only re-checked as each IQ frame is processed,
        # so silence has to keep arriving in real time (not one queued
        # burst) for idle_s to actually elapse between checks. Also gives
        # PeakDetector's own history window (10 updates) room to roll the
        # tone's earlier hits off before it stops reporting the peak.
        async def _feed_silence():
            seed = 100
            while True:
                stream.push(_iq_frame_bytes([], FS, FRAME_SAMPLES, noise_std=0.02, seed=seed))
                seed += 1
                await asyncio.sleep(0.01)

        feeder = asyncio.create_task(_feed_silence())
        try:
            assert await _wait_until(lambda: band.status()["channels"] == [], timeout_s=5.0)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_event_is_tagged_with_band_source(monkeypatch):
    memory_freq = CENTER_HZ - 12_500
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[(memory_freq, "sig-1", [])]
    )
    await band.start()
    try:
        backend = backends["pocsag"]
        backend.push(
            DecodeEvent(ts=1_700_000_000.0, decoder="pocsag", kind="text", text="hello", fields={})
        )
        assert await _wait_until(lambda: len(events) == 1)
        event = events[0]
        assert event.fields["source"] == "band"
        assert event.frequency_hz == int(memory_freq)
        assert event.signal_id == "sig-1"
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_retune_drops_detected_keeps_memory(monkeypatch):
    memory_freq = CENTER_HZ - 12_500
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[(memory_freq, "sig-1", [])], threshold_db=25.0
    )
    await band.start()
    try:
        tone_offset = 2 * SPACING_HZ
        for seed in range(6):
            stream.push(
                _iq_frame_bytes([tone_offset], FS, FRAME_SAMPLES, noise_std=0.02, seed=seed)
            )

        def has_detected():
            return any(c["source"] == "detected" for c in band.status()["channels"])

        assert await _wait_until(has_detected)
        assert len(band.status()["channels"]) == 2

        await band.retune(CENTER_HZ, FS, [(memory_freq, "sig-1", [])])

        status = band.status()
        assert len(status["channels"]) == 1
        assert status["channels"][0]["source"] == "memory"
        assert status["channels"][0]["signal_id"] == "sig-1"
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_live_retune_rejects_dequeued_and_upstream_stale_frames(monkeypatch):
    band, stream, _backends, _events, _statuses = _make_band(monkeypatch, memories=[], detect=False)
    await band.start()
    try:
        old_generation = band._span_generation
        old_frame = SimpleNamespace(data=b"", received_at=time.time() - 1)

        # Put retune ahead of a frame already removed from the queue but
        # waiting for the pump lock. The frame's arrival epoch must survive
        # that wait and be checked after retune releases the lock.
        await band._lock.acquire()
        retune = asyncio.create_task(band.retune(CENTER_HZ + 1_000_000, FS, []))
        await asyncio.sleep(0)
        in_flight = asyncio.create_task(
            band._pump_queued_frame(_QueuedIQFrame(old_frame, old_generation))
        )
        await asyncio.sleep(0)
        band._queue.put_nowait(_QueuedIQFrame(old_frame, old_generation))
        band._lock.release()

        await retune
        await in_flight
        assert band._frames == 0
        assert band._queue.empty()  # the queued old-span frame was flushed

        # SDRconnect's socket queue can still deliver a frame read before the
        # confirmed tune. The receive-time barrier must reject that as well.
        dropped = band._dropped
        stream.push(b"", received_at=time.time() - 1)
        assert await _wait_until(lambda: band._dropped > dropped)
        assert band._queue.empty()
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_replay_generation_keeps_queued_frames_from_new_generation(monkeypatch):
    band, _stream, _backends, _events, _statuses = _make_band(
        monkeypatch, memories=[], detect=False, track_generation=True
    )
    await band.start()
    try:
        band._pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await band._pump_task
        band._pump_task = None

        band._generation = 4
        retained = SimpleNamespace(generation=4)
        stale = SimpleNamespace(generation=3)
        band._queue.put_nowait(retained)
        band._queue.put_nowait(stale)
        await band._on_new_generation()

        assert band._queue.get_nowait() is retained
        assert band._queue.empty()
        assert band.stale_frames == 1
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_detect_false_skips_peak_detector(monkeypatch):
    memory_freq = CENTER_HZ - 12_500
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[(memory_freq, "sig-1", [])], detect=False
    )
    assert band._peaks is None
    await band.start()
    try:
        # Even a strong tone must never turn into a "detected" channel:
        # max_channels=0 already prevents it, but detect=False means the
        # peak detector (and its FFT) never runs at all.
        tone_offset = 2 * SPACING_HZ
        for seed in range(6):
            stream.push(
                _iq_frame_bytes([tone_offset], FS, FRAME_SAMPLES, noise_std=0.02, seed=seed)
            )
        await asyncio.sleep(0.1)
        status = band.status()
        assert len(status["channels"]) == 1
        assert status["candidates"] == []
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_detect_true_builds_peak_detector(monkeypatch):
    band, stream, backends, events, statuses = _make_band(monkeypatch, memories=[])
    assert band._peaks is not None


@pytest.mark.asyncio
async def test_iq_connected_reflects_recent_frame(monkeypatch):
    band, stream, backends, events, statuses = _make_band(monkeypatch, memories=[])
    fake_now = [1_000.0]
    monkeypatch.setattr(band, "_clock", lambda: fake_now[0])
    await band.start()
    try:
        assert band.status()["iq"]["connected"] is False

        stream.push(_iq_frame_bytes([], FS, FRAME_SAMPLES, noise_std=0.01))
        assert await _wait_until(lambda: band.status()["iq"]["connected"] is True)

        fake_now[0] += 2.1
        assert band.status()["iq"]["connected"] is False
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_source_tag_overrides_event_field(monkeypatch):
    memory_freq = CENTER_HZ - 12_500
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[(memory_freq, "sig-1", [])], source_tag="iq"
    )
    await band.start()
    try:
        backend = backends["pocsag"]
        backend.push(
            DecodeEvent(ts=1_700_000_000.0, decoder="pocsag", kind="text", text="hello", fields={})
        )
        assert await _wait_until(lambda: len(events) == 1)
        assert events[0].fields["source"] == "iq"
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_pin_adds_channel_while_running(monkeypatch):
    band, stream, backends, events, statuses = _make_band(monkeypatch, memories=[])
    await band.start()
    try:
        pin_freq = CENTER_HZ + 5 * SPACING_HZ
        await band.pin(pin_freq, "sig-pin", ["pocsag"])
        status = band.status()
        assert len(status["channels"]) == 1
        channel = status["channels"][0]
        assert channel["source"] == "pinned"
        assert channel["signal_id"] == "sig-pin"
        assert channel["decoders"] == ["pocsag"]
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_pinned_channel_survives_idle_retirement(monkeypatch):
    # idle_s tiny enough that a detected channel would be retired almost
    # immediately -- a pinned one must not be, since _retire_idle only ever
    # looks at source == "detected". detect=False keeps noise from being
    # picked up as spurious detected channels of its own.
    band, stream, backends, events, statuses = _make_band(
        monkeypatch, memories=[], idle_s=0.01, detect=False
    )
    await band.start()
    try:
        pin_freq = CENTER_HZ + 5 * SPACING_HZ
        await band.pin(pin_freq, None, ["pocsag"])
        for seed in range(3):
            stream.push(_iq_frame_bytes([], FS, FRAME_SAMPLES, noise_std=0.01, seed=seed))
            await asyncio.sleep(0.02)
        status = band.status()
        assert len(status["channels"]) == 1
        assert status["channels"][0]["source"] == "pinned"
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_unpin_removes_channel(monkeypatch):
    band, stream, backends, events, statuses = _make_band(monkeypatch, memories=[])
    await band.start()
    try:
        pin_freq = CENTER_HZ + 5 * SPACING_HZ
        await band.pin(pin_freq, None, ["pocsag"])
        assert len(band.status()["channels"]) == 1
        await band.unpin(pin_freq)
        assert band.status()["channels"] == []
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_retune_keeps_pinned_channel_inside_new_span(monkeypatch):
    band, stream, backends, events, statuses = _make_band(monkeypatch, memories=[])
    await band.start()
    try:
        pin_freq = CENTER_HZ + 5 * SPACING_HZ
        await band.pin(pin_freq, "sig-pin", ["pocsag"])

        await band.retune(CENTER_HZ, FS, [])

        status = band.status()
        assert len(status["channels"]) == 1
        assert status["channels"][0]["source"] == "pinned"
        assert status["channels"][0]["signal_id"] == "sig-pin"
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_channel_builds_one_nfm_per_distinct_input_rate(monkeypatch):
    # pocsag (22,050 Hz) and dmr (48,000 Hz) sharing one channel must each get
    # fed from their own NFMChannel, not a single one at the wrong rate.
    memory_freq = CENTER_HZ - 12_500
    band, stream, backends, events, statuses = _make_band(
        monkeypatch,
        memories=[(memory_freq, "sig-1", ["pocsag", "dmr"])],
        decoders=("pocsag", "dmr"),
    )
    await band.start()
    try:
        k = next(iter(band._channels))
        channel = band._channels[k]
        assert set(channel.nfm_by_rate.keys()) == {22050, 48000}
        assert channel.rate_by_decoder == {"pocsag": 22050, "dmr": 48000}
        assert "pocsag" in backends and "dmr" in backends

        stream.push(_iq_frame_bytes([], FS, FRAME_SAMPLES, noise_std=0.05))
        assert await _wait_until(lambda: backends["pocsag"].fed and backends["dmr"].fed)
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_replay_sample_gap_restarts_band_demod_and_decoder(monkeypatch):
    band, stream, backends, events, statuses = _make_band(
        monkeypatch,
        memories=[(CENTER_HZ - 12_500, "sig-1", [])],
        detect=False,
        track_generation=True,
    )
    await band.start()
    try:
        k = next(iter(band._channels))
        channel = band._channels[k]
        old_backend = channel.backends["pocsag"]
        old_nfm = channel.nfm_by_rate[22_050]
        block = _iq_frame_bytes([], FS, FRAME_SAMPLES, noise_std=0.1)

        class Frame:
            pass

        first = Frame()
        first.data, first.generation, first.sample_index, first.run_index = block, 0, 0, 3
        await band._pump_frame(first)

        # One frame was lost upstream: same source generation, index jumps by
        # two frames. The protocol backend and filterbank must both restart.
        dropped = Frame()
        dropped.data = block
        dropped.generation = 0
        dropped.sample_index = FRAME_SAMPLES * 2
        dropped.run_index = 3
        await band._pump_frame(dropped)

        channel = band._channels[k]
        assert band.status()["iq"]["dropped_frame_gaps"] == 1
        assert channel.backends["pocsag"] is not old_backend
        assert old_backend.stopped
        assert channel.nfm_by_rate[22_050] is not old_nfm
        assert channel.last_input == (0, FRAME_SAMPLES * 2, 3)
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_live_iq_queue_overflow_restarts_band_demod_and_decoder(monkeypatch):
    band, stream, backends, events, statuses = _make_band(
        monkeypatch,
        memories=[(CENTER_HZ - 12_500, "sig-1", [])],
        detect=False,
    )
    await band.start()
    try:
        # Pause consumption, then fill the bounded queue so the next live IQ
        # frame forces _read_frames() to discard one without sample indices.
        band._pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await band._pump_task
        band._pump_task = None

        class QueuedFrame:
            data = b""

        for _ in range(band._queue.maxsize):
            band._queue.put_nowait(QueuedFrame())

        block = _iq_frame_bytes([], FS, FRAME_SAMPLES, noise_std=0.1)
        stream.push(block)
        assert await _wait_until(lambda: band._dropped == 1)
        assert band._pending_frame_gap

        k = next(iter(band._channels))
        channel = band._channels[k]
        old_backend = channel.backends["pocsag"]
        old_nfm = channel.nfm_by_rate[22_050]
        next_frame = QueuedFrame()
        next_frame.data = block
        await band._pump_frame(next_frame)

        channel = band._channels[k]
        assert band.status()["iq"]["dropped_frame_gaps"] == 1
        assert not band._pending_frame_gap
        assert channel.backends["pocsag"] is not old_backend
        assert old_backend.stopped
        assert channel.nfm_by_rate[22_050] is not old_nfm
    finally:
        await band.stop()


@pytest.mark.asyncio
async def test_retune_drops_pinned_channel_outside_new_span(monkeypatch):
    band, stream, backends, events, statuses = _make_band(monkeypatch, memories=[])
    await band.start()
    try:
        pin_freq = CENTER_HZ + 5 * SPACING_HZ
        await band.pin(pin_freq, "sig-pin", ["pocsag"])

        far_center = CENTER_HZ + FS * 10
        await band.retune(far_center, FS, [])

        assert band.status()["channels"] == []
    finally:
        await band.stop()
