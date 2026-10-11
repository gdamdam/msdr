"""SpectrumHub: refcounted enable/disable, fan-out to every consumer, drop-
oldest backpressure, re-enable after a reconnect."""

import asyncio

from msdr.survey.spectrum_hub import SpectrumHub


async def test_acquire_enables_stream_once_for_two_consumers(adapter):
    hub = SpectrumHub(adapter)
    await hub.start()
    hub.acquire()
    await asyncio.sleep(0)
    hub.acquire()
    await asyncio.sleep(0)
    assert hub.consumers == 2
    assert adapter.spectrum_calls == [True]  # only the first acquire enables it
    await hub.stop()


async def test_release_disables_only_after_the_last_consumer(adapter):
    hub = SpectrumHub(adapter)
    await hub.start()
    q1 = hub.acquire()
    q2 = hub.acquire()
    await asyncio.sleep(0)

    hub.release(q1)
    await asyncio.sleep(0)
    assert adapter.spectrum_calls == [True]  # q2 still holds it

    hub.release(q2)
    await asyncio.sleep(0)
    assert adapter.spectrum_calls == [True, False]
    await hub.stop()


async def test_reconnect_reenables_stream_while_consumers_remain(adapter):
    hub = SpectrumHub(adapter)
    await hub.start()
    hub.acquire()
    await asyncio.sleep(0)

    adapter.push_connection(False)
    await asyncio.sleep(0)
    assert hub.connected is False

    adapter.push_connection(True)
    await asyncio.sleep(0)
    assert hub.connected is True
    await asyncio.sleep(0)  # let the spawned re-enable task actually run
    assert adapter.spectrum_calls == [True, True]
    await hub.stop()


async def test_reconnect_with_no_consumers_does_not_reenable(adapter):
    hub = SpectrumHub(adapter)
    await hub.start()
    adapter.push_connection(False)
    adapter.push_connection(True)
    await asyncio.sleep(0)
    assert adapter.spectrum_calls == []
    await hub.stop()


async def test_fanout_drops_oldest_frame_past_the_queue_limit(adapter):
    hub = SpectrumHub(adapter)
    await hub.start()
    q = hub.acquire()
    await asyncio.sleep(0)

    for i in range(60):
        adapter.push_frame(bytes([i % 256] * 512))
    await asyncio.sleep(0.05)

    assert q.qsize() == 50
    first = q.get_nowait()
    assert first.bins[0] == 10  # frames 0..9 dropped, 10..59 kept
    await hub.stop()


async def test_two_consumers_each_see_every_frame(adapter):
    hub = SpectrumHub(adapter)
    await hub.start()
    q1 = hub.acquire()
    q2 = hub.acquire()
    await asyncio.sleep(0)

    adapter.push_frame(bytes([42] * 512))
    await asyncio.sleep(0.02)

    assert q1.get_nowait().bins[0] == 42
    assert q2.get_nowait().bins[0] == 42
    await hub.stop()
