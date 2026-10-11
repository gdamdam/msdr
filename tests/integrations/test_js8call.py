import asyncio
import json

import pytest

from msdr.integrations.js8call import JS8CallClient, normalize_js8call_packet


def test_normalize_only_emitted_decoded_receive_text():
    packet = {
        "type": "RX.DIRECTED",
        "params": {
            "_ID": 123,
            "UTC": 1_700_000_000_000,
            "FROM": "K1ABC",
            "TO": "@ALLCALL",
            "TEXT": "HELLO WORLD",
            "FREQ": 7_078_420,
            "DIAL": 7_078_000,
            "SNR": -12,
            "GRID": "FN31",
        },
    }
    message = normalize_js8call_packet(packet)
    assert message["text"] == "HELLO WORLD"
    assert message["from"] == "K1ABC"
    assert message["frequency_hz"] == 7_078_420
    assert message["provenance"].startswith("Decoded text reported by JS8Call")
    packet["params"]["SNR"] = float("nan")
    assert normalize_js8call_packet(packet)["snr_db"] is None
    assert normalize_js8call_packet({"type": [], "params": {"TEXT": "ignored"}}) is None
    assert normalize_js8call_packet({"type": "TX.FRAME", "params": {"TEXT": "outgoing"}}) is None
    assert normalize_js8call_packet({"type": "RX.ACTIVITY", "params": {"TEXT": " "}}) is None


@pytest.mark.asyncio
async def test_client_reads_json_lines_from_local_tcp_server():
    received = []
    server_ready = asyncio.Event()

    async def handle(reader, writer):
        writer.write(
            (
                json.dumps(
                    {
                        "type": "RX.DIRECTED",
                        "params": {
                            "FROM": "N0CALL",
                            "TEXT": "LOCAL TEST",
                            "UTC": 1_700_000_000_000,
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        server_ready.set()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def on_event(event):
        received.append(event)

    client = JS8CallClient("127.0.0.1", port, on_event=on_event, reconnect_s=0.01)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(server_ready.wait(), 1)
        for _ in range(50):
            if any(event.get("type") == "integration_message" for event in received):
                break
            await asyncio.sleep(0.01)
        messages = [
            event["message"] for event in received if event["type"] == "integration_message"
        ]
        assert any(message["text"] == "LOCAL TEST" for message in messages)
        assert client.status()["connected"] is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.close()
        await server.wait_closed()


def test_documented_activity_and_heartbeat_value_fields():
    activity = normalize_js8call_packet(
        {
            "type": "RX.ACTIVITY",
            "value": "HC5PH: ",
            "params": {"FREQ": 7080420, "UTC": 1769740328005, "SNR": -22},
        }
    )
    assert activity["text"] == "HC5PH:"
    assert activity["frequency_hz"] == 7080420
    assert activity["from"] is None  # do not invent identity from arbitrary text
    heartbeat = normalize_js8call_packet(
        {
            "type": "RX.DIRECTED",
            "value": "KE2DMC: @HB HEARTBEAT",
            "params": {"TEXT": "", "FROM": "KE2DMC", "TO": "@HB", "UTC": 1769740226361},
        }
    )
    assert heartbeat["text"] == "KE2DMC: @HB HEARTBEAT"
    assert heartbeat["from"] == "KE2DMC"
