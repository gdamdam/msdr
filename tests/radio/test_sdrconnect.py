import asyncio
import contextlib
import json

import pytest
import pytest_asyncio

from msdr.decoder.audio_stream import AudioStream
from msdr.radio import readiness
from msdr.radio.fake_server import FakeSDRconnect
from msdr.radio.protocol import ConnectionChanged, PropertyChanged, SpectrumFrame
from msdr.radio.sdrconnect import SDRconnectAdapter
from msdr.recorder.iq_stream import IQStream


@pytest_asyncio.fixture
async def server():
    # Ephemeral port (0), so there is no real SDRconnect to shadow and
    # reuse_address=True is safe. It also matters for the tests that restart
    # the server on the same port: on Linux a socket accepted by a listener
    # without SO_REUSEADDR doesn't carry it, and while one is still closing
    # the rebind fails with "address already in use" (macOS allows it).
    srv = FakeSDRconnect(port=0)
    await srv.start(reuse_address=True)
    yield srv
    await srv.stop()


@pytest_asyncio.fixture
async def adapter(server):
    a = SDRconnectAdapter("127.0.0.1", server.port, backoff=(0.05, 0.2), startup_grace_s=0.0)
    await a.start()
    q = a.subscribe()
    first = await asyncio.wait_for(q.get(), 1.0)
    assert isinstance(first, ConnectionChanged) and first.connected is True
    yield a, q
    await a.stop()


async def test_get_roundtrip(server, adapter):
    a, _ = adapter
    assert await a.get("device_vfo_frequency") == 145_800_000

    server.numeric_values = True
    assert await a.get("device_center_frequency") == 145_800_000


async def test_get_property_response_from_secondary_does_not_answer_primary_read():
    adapter = SDRconnectAdapter("127.0.0.1", 5454, startup_grace_s=0.0)
    fut = asyncio.get_running_loop().create_future()
    adapter._pending["device_vfo_frequency"] = fut

    adapter._on_message(json.dumps({
        "event_type": "get_property_response",
        "property": "device_vfo_frequency",
        "value": "433920000",
        "device": "secondary",
    }))
    assert not fut.done()

    adapter._on_message(json.dumps({
        "event_type": "get_property_response",
        "property": "device_vfo_frequency",
        "value": "145800000",
        "device": "primary",
    }))
    assert fut.result() == 145_800_000
    adapter._pending.clear()


async def test_set_emits_property_changed_to_subscriber(adapter):
    a, q = adapter
    await a.set("device_vfo_frequency", 146_000_000)
    event = await asyncio.wait_for(q.get(), 1.0)
    assert event == PropertyChanged("device_vfo_frequency", 146_000_000, "primary", event.at)


async def test_apply_profile_broadcasts_each_property(server, adapter):
    a, q = adapter
    server.profiles["vhf_calling"] = {
        "device_vfo_frequency": 146_520_000,
        "demodulator": "NFM",
    }
    await a.apply_profile("vhf_calling")

    seen = {}
    for _ in range(2):
        event = await asyncio.wait_for(q.get(), 1.0)
        seen[event.property] = event.value
    assert seen == {"device_vfo_frequency": 146_520_000, "demodulator": "NFM"}


async def test_spectrum_stream_enable_emits_frames_then_stops(server, adapter):
    a, q = adapter
    server.spectrum_peaks = {145_800_000: 160}  # right at the seeded VFO/center
    await a.set_spectrum_stream(True)

    frame = None
    for _ in range(20):  # a handful of property_changed-free frames at ~20fps
        ev = await asyncio.wait_for(q.get(), 1.0)
        if isinstance(ev, SpectrumFrame):
            frame = ev
            break
    assert frame is not None
    assert len(frame.bins) == 512
    assert frame.bins[256] >= 150  # DC spike near bin 256

    await a.set_spectrum_stream(False)
    with pytest.raises(TimeoutError):
        while True:
            await asyncio.wait_for(q.get(), 0.2)


async def test_get_timeout_on_dropped_response(server, adapter):
    a, _ = adapter
    server.drop_get.add("lna_state")
    with pytest.raises(TimeoutError):
        await a.get("lna_state")


async def test_get_retries_after_timeout_clears_stale_pending(server, adapter):
    a, _ = adapter
    server.drop_get.add("lna_state")
    with pytest.raises(TimeoutError):
        await a.get("lna_state")

    # A prior lost response must not leave a stale pending future behind --
    # the next get() has to resend get_property rather than wait forever.
    server.drop_get.clear()
    assert await a.get("lna_state") == 3


async def test_reconnect_after_server_restart(server, adapter):
    a, q = adapter
    await server.stop()
    down = await asyncio.wait_for(q.get(), 3.0)
    assert isinstance(down, ConnectionChanged) and down.connected is False

    # Rebinding the exact same port right after closing it would otherwise hit
    # TIME_WAIT from the connection this test itself just closed (observed to
    # linger 20s+ on macOS) -- reuse_address=True is safe here since it's our
    # own just-released port, not a conflict with another process.
    await server.start(reuse_address=True)
    up = await asyncio.wait_for(q.get(), 3.0)
    assert isinstance(up, ConnectionChanged) and up.connected is True


async def test_retry_now_wakes_backoff_wait(server):
    # A long backoff means a reconnect within the assertion's timeout can only
    # happen via retry_now() waking the wait, not the natural backoff elapsing
    # -- proving retry_now() actually short-circuits it rather than the test
    # just getting lucky with timing.
    a = SDRconnectAdapter("127.0.0.1", server.port, backoff=(5.0, 5.0), startup_grace_s=0.0)
    await a.start()
    q = a.subscribe()
    try:
        first = await asyncio.wait_for(q.get(), 1.0)
        assert isinstance(first, ConnectionChanged) and first.connected is True

        await server.stop()
        down = await asyncio.wait_for(q.get(), 3.0)
        assert isinstance(down, ConnectionChanged) and down.connected is False
        assert a.retry_deadline is not None

        await server.start(reuse_address=True)
        a.retry_now()
        up = await asyncio.wait_for(q.get(), 1.0)
        assert isinstance(up, ConnectionChanged) and up.connected is True
        assert a.retry_deadline is None
    finally:
        await a.stop()


async def test_survives_handshake_error_and_keeps_retrying():
    # F5: a wrong port serving plain HTTP (or a WSS/ACL rejection) raises
    # websockets.exceptions.InvalidHandshake/InvalidStatus/InvalidMessage,
    # not ConnectionClosed/OSError. Before the fix that killed the reader
    # task silently -- no ConnectionChanged(False), no reconnect, ever.
    attempts = 0
    attempt_event = asyncio.Event()

    async def handle(reader, writer):
        nonlocal attempts
        attempts += 1
        attempt_event.set()
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    a = SDRconnectAdapter("127.0.0.1", port, backoff=(0.05, 0.1))
    await a.start()
    try:
        await asyncio.wait_for(attempt_event.wait(), 2.0)
        attempt_event.clear()
        # A second attempt proves the reader task survived the first
        # handshake failure and is still retrying, not dead.
        await asyncio.wait_for(attempt_event.wait(), 2.0)
        assert attempts >= 2
        assert a.connected is False
        assert not a._reader_task.done()
    finally:
        await a.stop()
        srv.close()
        await srv.wait_closed()


async def test_relaunch_gets_a_startup_grace_but_initial_attach_does_not(server, monkeypatch):
    # The shared minimum applies even when a caller requests a shorter grace,
    # but only once SDRconnect has actually gone down and come back -- the
    # very first attachment to an already-running instance must not stall.
    monkeypatch.setattr(readiness, "_FIRST_PROBE_DELAY_S", 0.05)
    monkeypatch.setattr(readiness, "_MIN_SETTLE_S", 0.4)
    a = SDRconnectAdapter("127.0.0.1", server.port, backoff=(0.05, 0.1), startup_grace_s=0.3)
    await a.start()
    q = a.subscribe()
    try:
        started = asyncio.get_running_loop().time()
        first = await asyncio.wait_for(q.get(), 2.0)
        assert isinstance(first, ConnectionChanged) and first.connected is True
        assert asyncio.get_running_loop().time() - started < 0.4

        await server.stop()
        down = await asyncio.wait_for(q.get(), 3.0)
        assert down.connected is False
        await server.start(reuse_address=True)
        started = asyncio.get_running_loop().time()
        up = await asyncio.wait_for(q.get(), 3.0)
        assert up.connected is True
        assert asyncio.get_running_loop().time() - started >= 0.4
    finally:
        await a.stop()


async def test_simultaneous_clients_probe_serially_and_stagger(server, monkeypatch):
    monkeypatch.setattr(readiness, "_FIRST_PROBE_DELAY_S", 0.01)
    monkeypatch.setattr(readiness, "_CLIENT_GAP_S", 0.12)
    monkeypatch.setattr(readiness, "_MIN_SETTLE_S", 0.0)
    original_probe = readiness._probe_once
    active = peak = 0

    async def slow_probe(ws, timeout_s):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.04)
            return await original_probe(ws, timeout_s)
        finally:
            active -= 1

    monkeypatch.setattr(readiness, "_probe_once", slow_probe)
    adapters = [
        SDRconnectAdapter("127.0.0.1", server.port, startup_grace_s=0.05)
        for _ in range(3)
    ]
    queues = [a.subscribe() for a in adapters]
    try:
        await asyncio.gather(*(a.start() for a in adapters))
        arrivals = []
        for q in queues:
            event = await asyncio.wait_for(q.get(), 2.0)
            assert isinstance(event, ConnectionChanged) and event.connected is True
            arrivals.append(event.at)
        assert peak == 1
        ordered = sorted(arrivals)
        assert all(b - a >= 0.1 for a, b in zip(ordered, ordered[1:], strict=False))
    finally:
        await asyncio.gather(*(a.stop() for a in adapters))


async def test_audio_and_iq_enable_after_the_shared_connection_gate(server, monkeypatch):
    monkeypatch.setattr(readiness, "_FIRST_PROBE_DELAY_S", 0.01)
    monkeypatch.setattr(readiness, "_CLIENT_GAP_S", 0.12)
    monkeypatch.setattr(readiness, "_MIN_SETTLE_S", 0.0)
    commands: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
    original_dispatch = server._dispatch

    async def record_dispatch(ws, msg):
        if msg.get("event_type") in {"audio_stream_enable", "iq_stream_enable"}:
            commands.put_nowait((msg["event_type"], asyncio.get_running_loop().time()))
        await original_dispatch(ws, msg)

    monkeypatch.setattr(server, "_dispatch", record_dispatch)
    streams = [
        AudioStream("127.0.0.1", server.port, startup_grace_s=0.05),
        IQStream("127.0.0.1", server.port, startup_grace_s=0.05),
    ]
    try:
        await asyncio.gather(*(stream.enable(True) for stream in streams))
        await asyncio.gather(*(stream.start() for stream in streams))
        first = await asyncio.wait_for(commands.get(), 2.0)
        second = await asyncio.wait_for(commands.get(), 2.0)
        assert {first[0], second[0]} == {"audio_stream_enable", "iq_stream_enable"}
        assert second[1] - first[1] >= 0.1
    finally:
        await asyncio.gather(*(stream.stop() for stream in streams))


async def test_missing_sample_rate_still_connects_with_no_device_selected(server, monkeypatch):
    # No device selected in SDRconnect is a valid, connectable state -- it
    # must not retry the handshake forever under the per-host lock.
    monkeypatch.setattr(readiness, "_FIRST_PROBE_DELAY_S", 0.01)
    monkeypatch.setattr(readiness, "_PROBE_INTERVAL_S", 0.01)
    monkeypatch.setattr(readiness, "_PROBE_TIMEOUT_S", 0.06)
    monkeypatch.setattr(readiness, "_MIN_SETTLE_S", 0.0)
    server.drop_get.add("device_sample_rate")
    adapter = SDRconnectAdapter(
        "127.0.0.1", server.port, backoff=(0.05, 0.05), startup_grace_s=0.01
    )
    queue = adapter.subscribe()
    try:
        await adapter.start()
        event = await asyncio.wait_for(queue.get(), 2.0)
        assert isinstance(event, ConnectionChanged) and event.connected is True
    finally:
        await adapter.stop()
