"""Satellite pass -> decoder link: table sanity, start/tag/stop at LOS (synthetic fakes)."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from msdr.api.services.sky import SkyServiceMixin
from msdr.decoder.backends import DecodeEvent
from msdr.decoder.manager import DecoderManager
from msdr.decoder.registry import DECODERS
from msdr.providers.sat_decode import SAT_DECODE_TABLE, decode_plan
from msdr.providers.tracking import DopplerLoop


def test_table_sanity():
    assert len({e.norad for e in SAT_DECODE_TABLE}) == len(SAT_DECODE_TABLE)
    for e in SAT_DECODE_TABLE:
        assert e.decoder in DECODERS, e
        assert 144_000_000 <= e.frequency_hz <= 438_000_000
    assert decode_plan(25544).decoder == "aprs"
    assert decode_plan(1) is None


class _Decoder:
    def __init__(self):
        self.event_tags = None
        self.stopped = []

    async def stop_decoding(self, reason):
        self.stopped.append(reason)


class _Loop:
    on_end = None

    async def stop(self):
        # Mirrors DopplerLoop._teardown: the hook runs once, then is cleared.
        on_end, self.on_end = self.on_end, None
        if on_end is not None:
            await on_end()

    async def start(self, signal_id, satellite, f0_hz, on_end=None):
        await self.stop()
        self.on_end = on_end
        self.f0_hz = f0_hz
        return {"signal_id": signal_id}


def _stub(satellite="ISS", norad=25544):
    decoder = _Decoder()
    started = []

    async def decoder_start(ids, hz):
        started.append((ids, hz, dict(decoder.event_tags)))

    async def publish_owner():
        pass

    stub = SimpleNamespace(
        _require_band_scan_idle=lambda: None,
        settings=SimpleNamespace(features=SimpleNamespace(tracking=True)),
        signals=SimpleNamespace(
            get=lambda _id: SimpleNamespace(satellite=satellite, frequency_hz=145_800_000)
        ),
        adapter=SimpleNamespace(connected=True),
        confirmed=SimpleNamespace(props={"started": True, "device_vfo_frequency": 145_825_000}),
        _executor=SimpleNamespace(running=False),
        _catalog_entry=lambda label: {"norad": norad},
        _require_decoder=lambda: None,
        decoder=decoder,
        decoder_start=decoder_start,
        doppler_loop=_Loop(),
        _publish_owner=publish_owner,
    )
    stub._end_pass_decode = lambda pass_id=None: SkyServiceMixin._end_pass_decode(stub, pass_id)
    return stub, started


async def test_pass_start_starts_decoder_with_tags_and_los_stops():
    stub, started = _stub()
    result = await SkyServiceMixin.start_tracking(stub, "sig", decode=True)

    ids, hz, tags = started[0]
    assert ids == ["aprs"] and hz == 145_825_000
    assert tags["satellite"] == "ISS" and tags["norad"] == 25544
    assert tags["pass_id"].startswith("25544-")
    assert result["decode"]["decoder"] == "aprs"
    assert stub.doppler_loop.f0_hz == 145_825_000

    await stub.doppler_loop.on_end()  # LOS
    assert stub.decoder.stopped == ["pass ended"]
    assert stub.decoder.event_tags is None


async def test_retrack_with_decode_keeps_new_decoder_and_tags():
    stub, started = _stub()
    await SkyServiceMixin.start_tracking(stub, "sig", decode=True)
    stub.decoder.stopped.clear()
    second = await SkyServiceMixin.start_tracking(stub, "sig", decode=True)
    # Old pass's hook stopped the old decoder before the new one started...
    assert len(started) == 2
    # ...and the new pass keeps its tags and is not stopped afterwards.
    assert second["decode"]["pass_id"] == stub.decoder.event_tags["pass_id"]
    assert stub.decoder.stopped == ["pass ended"]  # only the old pass's stop


async def test_stale_end_hook_ignored():
    stub, _ = _stub()
    stub.decoder.event_tags = {"pass_id": "new"}
    await SkyServiceMixin._end_pass_decode(stub, "old")
    assert stub.decoder.stopped == [] and stub.decoder.event_tags == {"pass_id": "new"}


async def test_no_decode_leaves_decoder_alone():
    stub, started = _stub()
    await SkyServiceMixin.start_tracking(stub, "sig")
    assert started == [] and stub.doppler_loop.on_end is None


async def test_decode_unknown_satellite_rejected():
    stub, started = _stub(norad=99999)
    with pytest.raises(HTTPException) as exc:
        await SkyServiceMixin.start_tracking(stub, "sig", decode=True)
    assert exc.value.status_code == 400 and started == []


async def test_doppler_loop_teardown_calls_on_end_once():
    calls = []

    class _Q:
        def subscribe(self):
            return asyncio.Queue()

        def unsubscribe(self, q):
            pass

    async def publish(_msg):
        pass

    service = SimpleNamespace(
        confirmed=_Q(),
        broadcaster=SimpleNamespace(publish=publish),
        settings=SimpleNamespace(doppler_interval_s=100),
    )
    loop = DopplerLoop(service)

    async def on_end():
        calls.append(1)

    await loop.start("sig", "ISS", 145_825_000, on_end=on_end)
    await loop.stop()
    await loop.stop()
    assert calls == [1]


def test_manager_tags_events():
    mgr = DecoderManager.__new__(DecoderManager)
    ev = DecodeEvent(ts=0.0, decoder="aprs", kind="text", text="x", fields={"a": 1})
    assert mgr._tag(ev) is ev
    mgr.event_tags = {"satellite": "ISS", "pass_id": "p"}
    tagged = mgr._tag(ev)
    assert tagged.fields == {"a": 1, "satellite": "ISS", "pass_id": "p"}
    assert replace(ev).fields == {"a": 1}  # original untouched
