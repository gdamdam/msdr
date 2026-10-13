import asyncio
from pathlib import Path

import pytest

from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.manager import DecoderManager
from msdr.engine.confirmed import ConfirmedState, StateChanged
from msdr.radio.protocol import PropertyChanged


class FakeBackend:
    """build_backend() double: no real subprocess, just a queue the test pushes to."""

    def __init__(self, decoder_id: str):
        self.decoder_id = decoder_id
        self.fed: list[bytes] = []
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()
        self._error: str | None = None

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
        pass

    @property
    def error(self) -> str | None:
        return self._error

    def push(self, event: DecodeEvent) -> None:
        self._events.put_nowait(event)


def _fake_build_backend(unavailable=frozenset()):
    """Returns (build_backend double, {decoder_id: FakeBackend}) -- lets a test
    control exactly which decoders "have" their binary without touching the
    filesystem or spawning a real process."""
    backends: dict[str, FakeBackend] = {}

    def _build(spec, binaries, calls_dir=None, fax_dir=None):
        if spec.id in unavailable:
            raise DecoderError(f"{spec.id}: missing binary (fake)")
        backend = FakeBackend(spec.id)
        backends[spec.id] = backend
        return backend

    return _build, backends


def _make_manager(
    monkeypatch, *, adapter, stream, repo, confirmed, published, settings, unavailable=frozenset()
):
    fake_build, backends = _fake_build_backend(unavailable)
    monkeypatch.setattr("msdr.decoder.manager.build_backend", fake_build)
    # Manager's own binary gate (checked before build_backend) always sees
    # every binary as present; per-decoder unavailability is simulated by
    # build_backend raising DecoderError instead (see _fake_build_backend).
    monkeypatch.setattr(
        "msdr.decoder.manager.resolve_binaries",
        lambda bin_dirs: {"multimon-ng": "/fake/multimon-ng", "minimodem": "/fake/minimodem"},
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
    return manager, backends


def test_manager_accepts_calls_dir_and_fax_dir_kwargs(
    adapter, stream, repo, confirmed, published, settings
):
    # Stream B passes both explicitly (service.py); a bare DecoderManager()
    # falls back to the plan's default paths, created lazily by whichever
    # backend needs them (dsd-fme's WAVs / the upcoming radiofax PNGs).
    manager = DecoderManager(
        adapter=adapter,
        stream_factory=lambda: stream,
        repo=repo,
        publish=published.append,
        settings_getter=lambda: settings,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
        calls_dir="custom/calls",
        fax_dir="custom/fax",
    )
    assert manager._calls_dir == Path("custom/calls")
    assert manager._fax_dir == Path("custom/fax")

    default_manager = DecoderManager(
        adapter=adapter,
        stream_factory=lambda: stream,
        repo=repo,
        publish=published.append,
        settings_getter=lambda: settings,
        confirmed_state=confirmed,
        feature_enabled=lambda: True,
    )
    assert default_manager._calls_dir == Path("decoder/calls")
    assert default_manager._fax_dir == Path("decoder/fax")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rate,center,reason",
    [
        (48_000, 1090000000, "needs IQ rate"),
        (2_400_000, 1089500000, "full signal bandwidth"),
    ],
)
async def test_standalone_adsb_requires_sufficient_original_iq(
    monkeypatch,
    adapter,
    stream,
    repo,
    confirmed,
    published,
    settings,
    rate,
    center,
    reason,
):
    manager, _ = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    monkeypatch.setattr(
        "msdr.decoder.manager.resolve_binaries",
        lambda dirs: {"dump1090": "/fake/dump1090", "jt9": None},
    )
    manager._iq_stream_factory = lambda: stream
    adapter.props.update(device_center_frequency=center, device_sample_rate=rate)
    with pytest.raises(DecoderError, match=reason):
        await manager.start_decoding(["adsb"], 1090000000, "manual")
    assert not stream.started
    assert not manager.status()["active"]


@pytest.mark.asyncio
async def test_squelch_disabled_on_start_and_restored_on_stop(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    adapter.props["squelch_enable"] = True
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert status["active"] is True
        assert adapter.props["squelch_enable"] is False
        assert ("squelch_enable", False) in adapter.sets

        await manager.stop_decoding("user")
        assert adapter.props["squelch_enable"] is True
        assert adapter.sets[-1] == ("squelch_enable", True)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_retune_stops_decoding_with_reason(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "memory", signal_id="sig-1")
        assert manager.status()["active"] is True

        # Bandwidth is 12500 Hz (adapter fixture) -> half-bandwidth is 6250 Hz;
        # a 50 kHz jump is well past that, so this must be treated as a retune.
        confirmed.emit(
            StateChanged(
                property="device_vfo_frequency",
                old=152_840_000,
                new=152_890_000,
                cause="external",
                at=0.0,
            )
        )
        # The consumer stops decoding asynchronously; give it real time, not ticks.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not manager.status()["active"]:
                break

        status = manager.status()
        assert status["active"] is False
        assert status["last_stop_reason"] == "retuned"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_user_stop_clears_pass_event_tags(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        manager.event_tags = {"pass_id": "p1"}
        await manager.start_decoding(["pocsag"], 152_840_000, "memory", signal_id="sig-1")
        await manager.stop_decoding("user")
        assert manager.event_tags is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unavailable_decoder_warns_but_others_start(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
        unavailable=frozenset({"rtty"}),
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["pocsag", "rtty"], 152_840_000, "manual")
        assert status["active"] is True
        assert any("rtty" in w for w in status["warnings"])
        assert [d["id"] for d in status["decoders"]] == ["pocsag"]
        assert "rtty" not in backends
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_events_are_persisted_and_published(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "memory", signal_id="sig-1")
        backend = backends["pocsag"]
        backend.push(
            DecodeEvent(
                ts=1_700_000_000.0,
                decoder="pocsag",
                kind="text",
                text="ALPHA - Test Page #00042",
                fields={"address": 100001, "function": 2, "demod": "POCSAG1200"},
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if repo.rows:
                break

        assert len(repo.rows) == 1
        row = repo.rows[0]
        assert row["text"] == "ALPHA - Test Page #00042"
        assert row["frequency_hz"] == 152_840_000
        assert row["signal_id"] == "sig-1"

        event_msgs = [m for m in published if m.get("type") == "decoder_event"]
        assert len(event_msgs) == 1
        assert event_msgs[0]["event"] == row
        assert manager.status()["events_total"] == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_image_update_of_path_merges_into_stored_start_event(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    """wefax's "fax started" event is stored once; its finishing event
    carries update_of_path instead of a fresh image_path, and the manager
    must turn that into repo.update_event() on the row stored at start."""
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        backend = backends["pocsag"]

        backend.push(
            DecodeEvent(
                ts=1.0,
                decoder="wefax",
                kind="image",
                text="fax started",
                fields={"lpm": 120, "ioc": 576},
                image_path="/tmp/fax/abc123.png",
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if repo.rows:
                break
        assert len(repo.rows) == 1
        stored_id = repo.rows[0]["id"]

        backend.push(
            DecodeEvent(
                ts=2.0,
                decoder="wefax",
                kind="image",
                text="fax 120 lpm IOC 576 · 40 lines",
                fields={"lpm": 120, "ioc": 576, "lines": 40},
                update_of_path="/tmp/fax/abc123.png",
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if any(m.get("type") == "decoder_event_update" for m in published):
                break

        assert len(repo.rows) == 1  # merged in place, not a second row
        assert repo.rows[0]["id"] == stored_id
        assert repo.rows[0]["text"] == "fax 120 lpm IOC 576 · 40 lines"
        assert repo.rows[0]["fields"]["lines"] == 40

        update_msgs = [m for m in published if m.get("type") == "decoder_event_update"]
        assert len(update_msgs) == 1
        assert update_msgs[0]["event"]["id"] == stored_id
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_start_decoding_raises_when_nothing_available(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
        unavailable=frozenset({"pocsag"}),
    )
    await manager.start()
    try:
        with pytest.raises(DecoderError):
            await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert manager.status()["active"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_agc_disabled_on_start_and_restored_on_stop(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    # Live test: SDRconnect's AGC re-gains at burst onset and garbles the
    # first codewords of a POCSAG/FLEX transmission -- same
    # disable-while-decoding, restore-on-stop treatment as squelch.
    adapter.props["agc_enable"] = True
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert status["active"] is True
        assert adapter.props["agc_enable"] is False
        assert ("agc_enable", False) in adapter.sets

        await manager.stop_decoding("user")
        assert adapter.props["agc_enable"] is True
        assert adapter.sets[-1] == ("agc_enable", True)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_agc_left_alone_when_already_disabled(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    # The fake adapter is dict-backed; an unseeded "agc_enable" comes back as
    # None, which must be treated as off (no set() call either way).
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert not any(prop == "agc_enable" for prop, _ in adapter.sets)
        await manager.stop_decoding("user")
        assert not any(prop == "agc_enable" for prop, _ in adapter.sets)
    finally:
        await manager.stop()


class FakeIQStream:
    """IQStream double for band-decode tests: no frames ever arrive, just
    enough to exercise BandDecoder's lifecycle (rejection paths don't even
    reach this; "status has band" does)."""

    def __init__(self):
        self.connected = True
        self.stopped = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    async def enable(self, on: bool) -> None:
        pass

    async def frames(self):
        while True:
            yield await self._queue.get()


@pytest.mark.asyncio
async def test_start_band_rejects_non_nfm_family_decoder(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        with pytest.raises(DecoderError):
            await manager.start_band(["rtty"])
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_start_band_refuses_when_receiver_stopped(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    adapter.props["started"] = False
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        with pytest.raises(DecoderError):
            await manager.start_band(["pocsag"])
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_status_has_band(monkeypatch, adapter, stream, repo, confirmed, published, settings):
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    assert manager.status()["band"]["active"] is False

    adapter.props["device_center_frequency"] = 152_800_000
    adapter.props["device_sample_rate"] = 2_000_000
    band_stream = FakeIQStream()
    monkeypatch.setattr(manager, "_iq_stream_factory", lambda: band_stream)

    await manager.start()
    try:
        status = await manager.start_band(["pocsag"])
        assert status["band"]["active"] is True
        assert status["band"]["decoders"] == ["pocsag"]
        assert status["band"]["center_hz"] == 152_800_000

        stopped = await manager.stop_band("user")
        assert stopped["band"]["active"] is False
        assert stopped["band"]["last_stop_reason"] == "user"
    finally:
        await manager.stop()


def _band_pin_manager(monkeypatch, *, adapter, stream, repo, confirmed, published, settings):
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    # band_pin/start_band build their BandDecoder(s) from band.py, which
    # imports its own build_backend reference -- _make_manager above only
    # patches manager.py's, so a pinned/detected/memory channel would try to
    # spawn a real subprocess without this too.
    fake_build, band_backends = _fake_build_backend()
    monkeypatch.setattr("msdr.decoder.band.build_backend", fake_build)
    adapter.props["device_center_frequency"] = 152_800_000
    adapter.props["device_sample_rate"] = 2_000_000
    band_stream = FakeIQStream()
    monkeypatch.setattr(manager, "_iq_stream_factory", lambda: band_stream)
    return manager, band_backends


@pytest.mark.asyncio
async def test_band_pin_starts_session_when_idle(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        assert manager.status()["band"]["active"] is False

        status = await manager.band_pin(152_840_000, ["pocsag"])

        assert status["band"]["active"] is True
        channels = status["band"]["channels"]
        assert len(channels) == 1
        assert channels[0]["source"] == "pinned"
        assert channels[0]["frequency_hz"] == 152_840_000
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_band_pin_outside_span_raises_when_idle(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    # adapter has device_center_frequency=152_800_000, device_sample_rate=2_000_000
    # (set by _band_pin_manager) -- half-span-minus-guard is 975 kHz; 2 MHz away
    # is well outside it, and no band session is running yet.
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        with pytest.raises(DecoderError, match="outside the current span"):
            await manager.band_pin(154_900_000, ["pocsag"])
        assert manager.status()["band"]["active"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_band_pin_outside_span_raises_against_running_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    # Once a band session is running, the guard uses *its* center/rate, not
    # whatever the adapter reports right now (which start_band already
    # snapshotted) -- change the adapter's props after start_band to prove
    # the check isn't re-reading them.
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_band(["pocsag"])
        adapter.props["device_center_frequency"] = 400_000_000
        adapter.props["device_sample_rate"] = 20_000_000

        with pytest.raises(DecoderError, match="outside the current span"):
            await manager.band_pin(154_900_000, ["pocsag"])
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_band_pin_adds_channel_to_running_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_band(["pocsag"])

        status = await manager.band_pin(152_840_000, ["pocsag"])

        channels = status["band"]["channels"]
        assert any(c["source"] == "pinned" for c in channels)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_band_unpin_last_channel_stops_pin_started_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.band_pin(152_840_000, ["pocsag"])
        assert manager.status()["band"]["active"] is True

        status = await manager.band_unpin(152_840_000)

        assert status["band"]["active"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_band_unpin_last_channel_keeps_user_started_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_band(["pocsag"])
        await manager.band_pin(152_840_000, ["pocsag"])

        status = await manager.band_unpin(152_840_000)

        assert status["band"]["active"] is True
    finally:
        await manager.stop()


# -- single-channel IQ path (NFM-family decoders bypass the audio chain) -----


def _make_iq_manager(monkeypatch, *, adapter, stream, repo, confirmed, published, settings):
    """Like _make_manager, but also wires a fake IQ stream + band.py's
    build_backend double, so start_decoding() can take the IQ path."""
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    fake_build, band_backends = _fake_build_backend()
    monkeypatch.setattr("msdr.decoder.band.build_backend", fake_build)
    iq_stream = FakeIQStream()
    monkeypatch.setattr(manager, "_iq_stream_factory", lambda: iq_stream)
    adapter.props.setdefault("device_center_frequency", 152_800_000)
    adapter.props.setdefault("device_sample_rate", 2_000_000)
    return manager, backends, band_backends, iq_stream


@pytest.mark.asyncio
async def test_nfm_only_request_uses_iq_path(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends, band_backends, iq_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert status["source_kind"] == "iq"
        assert status["active"] is True
        assert [d["id"] for d in status["decoders"]] == ["pocsag"]
        assert "pocsag" in band_backends
        # Audio path must be untouched: no stream, no squelch/AGC calls.
        assert stream.started is False
        assert stream.enabled is False
        assert adapter.sets == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cw_request_uses_audio_path(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    adapter.props["demodulator"] = "CW"
    manager, _backends, _band_backends, _iq_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["cw"], 152_840_000, "manual")
        assert status["source_kind"] == "audio"
        assert stream.started is True
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_frequency_outside_iq_span_falls_back_to_audio(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends, _band_backends, _iq_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        # center 152.8 MHz, 2 MHz span -> half-span-minus-guard is 975 kHz;
        # 2.1 MHz away is well outside it.
        status = await manager.start_decoding(["pocsag"], 154_900_000, "manual")
        assert status["source_kind"] == "audio"
        assert any("outside IQ span" in w for w in status["warnings"])
        assert stream.started is True
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_single_channel_source_override_forces_audio(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    settings.single_channel_source = "audio"
    manager, _backends, _band_backends, _iq_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert status["source_kind"] == "audio"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_iq_only_decoder_skipped_on_audio_path_with_warning(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    # packet9600 is iq_only; forcing the audio path (single_channel_source
    # override) must skip it with a warning rather than start it on audio,
    # while an ordinary decoder in the same request still runs.
    settings.single_channel_source = "audio"
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        status = await manager.start_decoding(["packet9600", "pocsag"], 152_840_000, "manual")
        assert status["source_kind"] == "audio"
        assert [d["id"] for d in status["decoders"]] == ["pocsag"]
        assert any("packet9600: needs the IQ path" in w for w in status["warnings"])
        assert "packet9600" not in backends
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_vfo_retune_stops_iq_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends, _band_backends, _iq_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert manager.status()["source_kind"] == "iq"

        confirmed.emit(
            StateChanged(
                property="device_vfo_frequency",
                old=152_840_000,
                new=152_890_000,
                cause="external",
                at=0.0,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not manager.status()["active"]:
                break

        status = manager.status()
        assert status["active"] is False
        assert status["last_stop_reason"] == "retuned"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_center_frequency_change_stops_iq_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends, _band_backends, _iq_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        assert manager.status()["source_kind"] == "iq"

        confirmed.emit(
            StateChanged(
                property="device_center_frequency",
                old=152_800_000,
                new=152_900_000,
                cause="external",
                at=0.0,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not manager.status()["active"]:
                break

        status = manager.status()
        assert status["active"] is False
        assert status["last_stop_reason"] == "retuned"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_receiver_stop_event_stops_single_and_band_iq_sessions(
    monkeypatch, adapter, stream, repo, published, settings
):
    confirmed = ConfirmedState()
    manager, _backends, _band_backends, single_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    band_stream = FakeIQStream()

    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        manager._iq_stream_factory = lambda: band_stream
        await manager.start_band(["pocsag"])
        assert manager.status()["active"] is True
        assert manager.status()["band"]["active"] is True

        confirmed.apply(PropertyChanged("started", False, "primary", 1.0))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not manager.status()["active"] and not manager.status()["band"]["active"]:
                break

        assert manager.status()["active"] is False
        assert manager.status()["last_stop_reason"] == "receiver stopped"
        assert manager.status()["band"]["active"] is False
        assert single_stream.stopped is True
        assert band_stream.stopped is True
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_band_state_read_disconnect_stops_session_but_keeps_consumer_alive(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends, _band_backends, band_stream = _make_iq_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_band(["pocsag"])
        original_get = adapter.get

        async def fail_center_read(prop):
            if prop == "device_center_frequency":
                raise ConnectionError("radio disconnected during retune")
            return await original_get(prop)

        adapter.get = fail_center_read
        confirmed.emit(
            StateChanged(
                property="device_center_frequency",
                old=152_800_000,
                new=152_900_000,
                cause="external",
                at=0.0,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not manager.status()["band"]["active"]:
                break

        assert manager.status()["band"]["active"] is False
        assert band_stream.stopped is True
        assert manager._consumer_task is not None
        assert not manager._consumer_task.done()

        adapter.get = original_get
        confirmed.emit(
            StateChanged(
                property="device_sample_rate",
                old=2_000_000,
                new=1_000_000,
                cause="external",
                at=0.1,
            )
        )
        for _ in range(100):
            if manager._queue.empty():
                break
            await asyncio.sleep(0.01)
        assert manager._queue.empty()
        assert not manager._consumer_task.done()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_status_carries_per_decoder_session_facts(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag", "aprs"], 144_390_000, "manual")
        backends["pocsag"].push(
            DecodeEvent(
                ts=10.0,
                decoder="pocsag",
                kind="text",
                text="page",
                fields={},
                integrity="unavailable",
            )
        )
        backends["aprs"].push(
            DecodeEvent(
                ts=11.0, decoder="aprs", kind="text", text="A>B:x", fields={}, integrity="passed"
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if len(repo.rows) == 2:
                break
        decoders = {d["id"]: d for d in manager.status()["decoders"]}
        pocsag, aprs = decoders["pocsag"], decoders["aprs"]
        for d in (pocsag, aprs):
            assert {"id", "state", "error", "events", "valid_frames", "last_event_ts"} <= set(d)
            assert d["synchronized"] is None  # neither backend reports sync
        assert (pocsag["events"], pocsag["valid_frames"]) == (1, 0)
        assert pocsag["session_state"] == "unverified_output"
        assert pocsag["last_event_ts"] == 10.0
        assert (aprs["events"], aprs["valid_frames"]) == (1, 1)
        assert aprs["session_state"] == "valid_frames"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_status_marks_demodulator_mismatch_unsupported_input(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    adapter.props["demodulator"] = "USB"
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    await manager.start()
    try:
        await manager.start_decoding(["pocsag"], 152_840_000, "manual")
        (pocsag,) = manager.status()["decoders"]
        assert pocsag["session_state"] == "unsupported_input"
        assert "USB" in pocsag["unsupported"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("decoder_id", ["adsb", "lora"])
async def test_live_protocol_iq_decoder_starts_with_decoder_settings(
    monkeypatch, adapter, stream, repo, confirmed, published, settings, decoder_id
):
    # Regression (2026-10-03): the live raw-IQ path passed `settings.decoder`
    # where the getter already returns DecoderSettings, so starting the
    # built-in ADS-B or LoRa decoder live raised AttributeError (HTTP 500).
    # The LoRa fields mirror DecoderSettings' defaults.
    settings.lora_spreading_factor, settings.lora_bandwidth_hz = 11, 250_000
    settings.lora_coding_rate, settings.lora_protocol = 1, "meshtastic"
    manager, _ = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    monkeypatch.setattr("msdr.decoder.manager.resolve_binaries", lambda dirs: {})
    manager._iq_stream_factory = lambda: stream
    freq = 1_090_000_000 if decoder_id == "adsb" else 906_875_000
    adapter.props.update(device_center_frequency=freq + 10_000, device_sample_rate=6_000_000)
    status = await manager.start_decoding([decoder_id], freq, "manual")
    assert status["active"] is True
    await manager.stop_decoding("user")


@pytest.mark.asyncio
async def test_concurrent_starts_are_serialized(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _make_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    inflight = 0
    max_inflight = 0
    real_start = stream.start

    async def slow_start():
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.02)
        await real_start()
        inflight -= 1

    stream.start = slow_start
    await manager.start()
    try:
        await asyncio.gather(
            manager.start_decoding(["pocsag"], 152_840_000, "manual"),
            manager.start_decoding(["pocsag"], 152_840_000, "manual"),
        )
        assert max_inflight == 1, "two starts overlapped; the loser's stream/backends leak"
        assert manager.status()["active"] is True
        await manager.stop_decoding("user")
        assert manager.status()["active"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_concurrent_band_pins_share_one_session(
    monkeypatch, adapter, stream, repo, confirmed, published, settings
):
    manager, _backends = _band_pin_manager(
        monkeypatch,
        adapter=adapter,
        stream=stream,
        repo=repo,
        confirmed=confirmed,
        published=published,
        settings=settings,
    )
    band_stream = manager._iq_stream_factory()
    starts = 0
    real_start = band_stream.start

    async def slow_start():
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.02)
        await real_start()

    band_stream.start = slow_start
    await manager.start()
    try:
        await asyncio.gather(
            manager.band_pin(152_840_000, ["pocsag"]),
            manager.band_pin(152_860_000, ["pocsag"]),
        )
        channels = manager.status()["band"]["channels"]
        assert starts == 1, "second pin must join the first session, not start another"
        assert sorted(c["frequency_hz"] for c in channels) == [152_840_000, 152_860_000]
    finally:
        await manager.stop()
