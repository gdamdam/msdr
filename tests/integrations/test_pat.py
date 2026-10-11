import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from msdr.integrations.pat import SEEN_LIMIT, PatClient, normalize_base_url


class FakePatHandler(BaseHTTPRequestHandler):
    calls = []
    messages = [{
        "MID": "MID-1",
        "Date": "2026-09-30T12:00:00Z",
        "From": {"Address": "K1ABC"},
        "To": [{"Address": "N0CALL"}],
        "Subject": "Status",
        "Unread": True,
    }]

    def do_GET(self):
        self.calls.append(("GET", self.path))
        if self.path == "/api/status":
            result = {"connected": False}
        elif self.path == "/api/mailbox/in":
            result = self.messages
        elif self.path == "/api/mailbox/in/MID-1":
            result = {**self.messages[0], "Body": "Received through fake local Pat"}
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
def fake_pat_server():
    FakePatHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_base_url_rejects_credentials_and_non_http():
    with pytest.raises(ValueError):
        normalize_base_url("ftp://127.0.0.1:8080")
    with pytest.raises(ValueError):
        normalize_base_url("http://user:pass@127.0.0.1:8080")


def test_seen_message_cache_is_bounded():
    client = PatClient("http://127.0.0.1:8080", on_event=lambda _event: _record([], _event))
    for index in range(SEEN_LIMIT + 10):
        client._remember(f"MID-{index}")
    assert len(client._seen) == SEEN_LIMIT
    assert "MID-0" not in client._seen
    assert f"MID-{SEEN_LIMIT + 9}" in client._seen


@pytest.mark.asyncio
async def test_client_reads_pat_status_mailbox_and_details(fake_pat_server):
    events = []
    statuses = 0
    client = PatClient(fake_pat_server, poll_s=0.02, on_event=lambda event: _record(events, event))
    task = asyncio.create_task(client.run())
    try:
        for _ in range(100):
            statuses = sum(event.get("type") == "integration_status" for event in events)
            if statuses >= 2:
                break
            await asyncio.sleep(0.01)
        assert client.status()["connected"] is True
        assert client.status()["inbox_count"] == 1
        messages = [
            event["message"]
            for event in events
            if event.get("type") == "integration_message"
        ]
        assert len(messages) == 1
        assert messages[0]["text"] == "Received through fake local Pat"
        assert messages[0]["from"] == "K1ABC"
        assert messages[0]["unread"] is True
        assert messages[0]["provenance"] == "Pat Winlink mailbox via Pat HTTP API"
        assert {method for method, _path in FakePatHandler.calls} == {"GET"}
        assert any(path == "/api/mailbox/in/MID-1" for _method, path in FakePatHandler.calls)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _record(events, event):
    events.append(event)
