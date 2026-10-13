import asyncio
import socket
from datetime import datetime

import pytest

from msdr.feeds.runtime import FeedsRuntime
from msdr.feeds.sbs import SbsServer, format_sbs
from msdr.settings import FeedsSettings, Settings

TS = datetime(2024, 1, 2, 3, 4, 5, 678000).timestamp()
STAMP = "2024/01/02,03:04:05.678,2024/01/02,03:04:05.678"


def line(tt, *cols):
    return f"MSG,{tt},1,1,A1B2C3,1,{STAMP}," + ",".join(cols)


def test_ident_msg1():
    got = format_sbs({"icao": "a1b2c3", "transmission_type": 1, "callsign": "UAL123 "}, TS)
    assert got == line(1, "UAL123", "", "", "", "", "", "", "", "0", "0", "0", "0")


def test_airborne_position_msg3():
    got = format_sbs(
        {"icao": "A1B2C3", "transmission_type": 3, "altitude_ft": 35000,
         "latitude": 40.123456, "longitude": -74.2},
        TS,
    )
    assert got == line(3, "", "35000", "", "", "40.12346", "-74.20000", "", "", "0", "0", "0", "0")


def test_velocity_msg4():
    got = format_sbs(
        {"icao": "A1B2C3", "transmission_type": 4, "ground_speed_kt": 450.4,
         "track_deg": 270.2, "vertical_rate_fpm": -64.0},
        TS,
    )
    assert got == line(4, "", "", "450", "270", "", "", "-64", "", "0", "0", "0", "0")


def test_squawk_msg6_with_flags():
    got = format_sbs(
        {"icao": "A1B2C3", "transmission_type": 6, "altitude_ft": 12000, "squawk": "7700",
         "alert": "-1", "emergency": "-1", "spi": "0"},
        TS,
    )
    assert got == line(6, "", "12000", "", "", "", "", "", "7700", "-1", "-1", "0", "0")


def test_surveillance_alt_and_ground_types():
    assert format_sbs({"icao": "A1B2C3", "transmission_type": 5, "altitude_ft": 900}, TS) == line(
        5, "", "900", "", "", "", "", "", "", "0", "0", "0", "0"
    )
    assert format_sbs({"icao": "A1B2C3", "transmission_type": 8, "on_ground": "-1"}, TS) == line(
        8, "", "", "", "", "", "", "", "", "0", "0", "0", "-1"
    )


def test_type_inferred_and_unusable_events_skipped():
    # The native decoder tags altitude-only decodes as position (3); SBS needs a type 5 there.
    got = format_sbs({"icao": "A1B2C3", "transmission_type": 3, "altitude_ft": 900}, TS)
    assert got.startswith("MSG,5,")
    assert format_sbs({"icao": "A1B2C3", "callsign": "X"}, TS).startswith("MSG,1,")
    assert format_sbs({"icao": "A1B2C3"}, TS) is None
    assert format_sbs({"callsign": "X"}, TS) is None


@pytest.mark.asyncio
async def test_server_delivers_to_all_clients():
    server = SbsServer("127.0.0.1", 0)
    await server.start()
    try:
        conns = [await asyncio.open_connection("127.0.0.1", server.bound_port) for _ in range(2)]
        for _ in range(50):
            if server.client_count == 2:
                break
            await asyncio.sleep(0.01)
        server.publish("MSG,1,hello")
        for reader, _ in conns:
            assert await asyncio.wait_for(reader.readline(), 2) == b"MSG,1,hello\r\n"
        assert server.status()["clients"] == 2
        for _, writer in conns:
            writer.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stalled_client_is_dropped_without_blocking():
    server = SbsServer("127.0.0.1", 0, queue_lines=4, slow_full_streak=3, drain_timeout_s=0.2)
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
    try:
        for _ in range(50):
            if server.client_count:
                break
            await asyncio.sleep(0.01)
        big = "x" * 65536  # never read: fills the socket buffers, then the queue
        for _ in range(2000):
            server.publish(big)  # synchronous: must never block
            await asyncio.sleep(0)
            if server.client_count == 0:
                break
        assert server.client_count == 0
        assert server.clients_dropped == 1
    finally:
        writer.close()
        await server.stop()


@pytest.mark.asyncio
async def test_runtime_off_by_default_and_forwards_when_enabled():
    assert Settings().feeds == FeedsSettings()
    assert not FeedsSettings().sbs_enabled and not FeedsSettings().nmea_enabled
    runtime = FeedsRuntime(FeedsSettings())
    await runtime.start()
    runtime.on_message({"type": "decoder_event", "event": {"decoder": "adsb", "fields": {}}})
    status = runtime.status()
    assert not status["sbs"]["listening"] and not status["nmea"]["active"]

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    await runtime.configure(FeedsSettings(sbs_enabled=True, sbs_port=port))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for _ in range(50):
            if runtime._sbs.client_count:
                break
            await asyncio.sleep(0.01)
        runtime.on_message({"type": "decoder_event", "event": {
            "decoder": "adsb", "ts": TS, "integrity": "passed",
            "fields": {"icao": "A1B2C3", "callsign": "UAL1", "transmission_type": 1}}})
        data = await asyncio.wait_for(reader.readline(), 2)
        assert data.startswith(b"MSG,1,1,1,A1B2C3,")
        writer.close()
    finally:
        await runtime.stop()
