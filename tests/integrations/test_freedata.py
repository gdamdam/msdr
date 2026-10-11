import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from msdr.integrations.freedata import SEEN_LIMIT, FreeDATAClient, normalize_base_url


class FakeFreeDATAHandler(BaseHTTPRequestHandler):
    calls = []
    identity = {"name": "FreeDATA API", "api_version": "test"}
    messages = [
        {
            "id": "RX-1",
            "timestamp": "2026-09-30T12:00:00Z",
            "origin": "K1ABC-0",
            "destination": "N0CALL-0",
            "direction": "receive",
            "body": "Received through fake FreeDATA",
            "attachments": ["file-1"],
            "is_read": False,
        },
        {
            "id": "TX-1",
            "timestamp": "2026-09-30T12:01:00Z",
            "direction": "transmit",
            "body": "outbound",
        },
    ]

    def do_GET(self):
        self.calls.append(("GET", self.path))
        if self.path == "/":
            result = self.identity
        elif self.path == "/modem/state":
            result = {
                "is_modem_running": True,
                "radio_status": True,
                "radio_frequency": "14093000",
                "radio_mode": "PKTUSB",
            }
        elif self.path == "/freedata/messages":
            result = {"total_messages": len(self.messages), "messages": self.messages}
        elif self.path == "/freedata/messages/RX-1/attachments":
            result = {"attachments": [{"filename": "report.txt", "file_size": 123}]}
        else:
            self.send_error(404)
            return
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.calls.append(("POST", self.path))
        self.send_error(405)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fake_freedata_server():
    FakeFreeDATAHandler.calls = []
    FakeFreeDATAHandler.identity = {"name": "FreeDATA API", "api_version": "test"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFreeDATAHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_base_url_rejects_credentials_and_paths():
    for url in (
        "ftp://127.0.0.1:5000",
        "http://user:pass@127.0.0.1:5000",
        "http://127.0.0.1:5000/api",
    ):
        with pytest.raises(ValueError):
            normalize_base_url(url)


def test_seen_message_cache_is_bounded():
    client = FreeDATAClient(on_event=lambda _event: _record([], _event))
    for index in range(SEEN_LIMIT + 10):
        client._remember(f"RX-{index}")
    assert len(client._seen) == SEEN_LIMIT
    assert "RX-0" not in client._seen


@pytest.mark.asyncio
async def test_client_reads_only_received_messages_and_attachment_metadata(fake_freedata_server):
    events = []
    client = FreeDATAClient(
        fake_freedata_server, poll_s=0.02, on_event=lambda event: _record(events, event)
    )
    task = asyncio.create_task(client.run())
    try:
        for _ in range(100):
            if sum(event.get("type") == "integration_status" for event in events) >= 2:
                break
            await asyncio.sleep(0.01)
        assert client.status()["connected"] is True
        assert client.status()["inbox_count"] == 1
        assert client.status()["unread_count"] == 1
        assert client.status()["modem_running"] is True
        assert client.status()["radio_frequency_hz"] == 14_093_000
        messages = [
            event["message"] for event in events if event.get("type") == "integration_message"
        ]
        assert len(messages) == 1
        assert messages[0]["source"] == "freedata"
        assert messages[0]["text"] == "Received through fake FreeDATA"
        assert messages[0]["attachment_names"] == ["report.txt"]
        assert {method for method, _path in FakeFreeDATAHandler.calls} == {"GET"}
        assert (
            sum(
                path == "/freedata/messages/RX-1/attachments"
                for _, path in FakeFreeDATAHandler.calls
            )
            == 1
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_wrong_server_is_reported_without_fetching_mailbox(fake_freedata_server):
    FakeFreeDATAHandler.identity = {"name": "Something else"}
    events = []
    client = FreeDATAClient(
        fake_freedata_server, poll_s=60, on_event=lambda event: _record(events, event)
    )
    task = asyncio.create_task(client.run())
    try:
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.01)
        assert client.status()["connected"] is False
        assert "not a FreeDATA API" in client.status()["error"]
        assert FakeFreeDATAHandler.calls == [("GET", "/")]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _record(events, event):
    events.append(event)
