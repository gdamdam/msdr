"""SDRconnect start-up gate, shared by every WebSocket client msdr opens.

A native SDRconnect 1.0.8 crash was observed soon after launch while msdr was
attached; its cause is not established. msdr's control, audio, and IQ clients
reconnect independently, so this gate limits the load they place on a newly
available server.

Before a client sends anything on a new connection it uses ready_connection():

- probe: `get_property device_sample_rate` until SDRconnect answers with a
  real rate; if it never does, we log a warning and treat the instance as
  ready anyway (no device selected is a valid, connectable state);
- grace + stagger: after a disconnect (mark_down()), wait until at least
  _MIN_SETTLE_S after the first successful probe before sending normal
  commands (unless the caller explicitly disables grace for tests). The
  very first attachment to a running instance is not delayed this way.
  Only one client opens, probes, and passes the gate at a time, with a short
  gap between clients; the settle wait itself happens after that lock is
  released, so it does not block other clients' handshakes.

State is per host:port (one SDRconnect instance) and is reset by mark_down()
whenever a client loses or fails to open its connection.
"""

import asyncio
import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager

from websockets.asyncio.client import connect

logger = logging.getLogger(__name__)

_PROBE_PROPERTY = "device_sample_rate"
_PROBE_INTERVAL_S = 0.5
_PROBE_TIMEOUT_S = 15.0
_FIRST_PROBE_DELAY_S = 1.0
_CLIENT_GAP_S = 0.75
_MIN_SETTLE_S = 10.0

# host:port -> connection gate and timing for one SDRconnect instance
_state: dict[str, dict] = {}


def _entry(host: str, port: int) -> dict:
    return _state.setdefault(
        f"{host}:{port}",
        {
            "ready_at": None,
            "lock": asyncio.Lock(),
            "next_client_at": 0.0,
            "settle_required": False,
        },
    )


def mark_down(host: str, port: int) -> None:
    """A client lost (or couldn't open) its connection: the next one waits
    out the start-up grace again once the instance is back."""
    entry = _entry(host, port)
    entry["ready_at"] = None
    entry["settle_required"] = True


@asynccontextmanager
async def ready_connection(host: str, port: int, startup_grace_s: float, **connect_kwargs):
    """Open one socket at a time and hand it to the caller after settling.

    The lock covers only the WebSocket handshake and probe. It is released
    before the settle wait (if any) and before the caller's receive loop, so
    neither can block other clients' handshakes.
    """
    entry = _entry(host, port)
    async with entry["lock"]:
        wait_s = entry["next_client_at"] - time.monotonic()
        if wait_s > 0:
            await asyncio.sleep(wait_s)
        ws = await connect(f"ws://{host}:{port}/", **connect_kwargs)
        try:
            settle_wait_s = await wait_ready(ws, host, port, startup_grace_s)
        except BaseException:
            await ws.close()
            raise
        entry["next_client_at"] = time.monotonic() + _CLIENT_GAP_S
    if settle_wait_s > 0:
        logger.info("SDRconnect just came back; waiting %.1f s before using it", settle_wait_s)
        await asyncio.sleep(settle_wait_s)
    try:
        yield ws
    finally:
        await ws.close()


async def _probe_once(ws, timeout_s: float) -> bool:
    await ws.send(json.dumps({"event_type": "get_property", "property": _PROBE_PROPERTY}))
    deadline = time.monotonic() + timeout_s
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            message = await asyncio.wait_for(ws.recv(), remaining)
        except TimeoutError:
            return False
        if isinstance(message, bytes):
            continue  # stray stream payload -- not an answer
        with contextlib.suppress(json.JSONDecodeError):
            msg = json.loads(message)
            if (
                msg.get("event_type") == "get_property_response"
                and msg.get("property") == _PROBE_PROPERTY
            ):
                try:
                    return float(msg.get("value")) > 0
                except (TypeError, ValueError):
                    return False
    return False


async def wait_ready(ws, host: str, port: int, startup_grace_s: float) -> float:
    """Probe a fresh connection until SDRconnect looks ready (see module doc).
    Messages read while probing are dropped -- nothing has been enabled yet.

    Returns the settle wait (seconds, possibly 0) the caller should sleep
    *after* releasing the per-host lock -- this never sleeps for settle
    itself, so the lock is only held for the handshake and probe.
    """
    entry = _entry(host, port)
    if startup_grace_s > 0 and entry["ready_at"] is None:
        # A WebSocket listener can come up before native radio objects do.
        # Avoid probing it immediately when we have just seen the port return.
        await asyncio.sleep(_FIRST_PROBE_DELAY_S)
    give_up = time.monotonic() + _PROBE_TIMEOUT_S
    while not await _probe_once(ws, _PROBE_INTERVAL_S * 2):
        if time.monotonic() >= give_up:
            # No device selected is a valid, connectable state -- proceed
            # rather than retrying this instance forever under the lock.
            logger.warning("SDRconnect not reporting a sample rate; continuing without grace")
            break
        await asyncio.sleep(_PROBE_INTERVAL_S)
    now = time.monotonic()
    if entry["ready_at"] is None:
        entry["ready_at"] = now
    if startup_grace_s <= 0 or not entry["settle_required"]:
        # Grace disabled for tests, or this is the very first attachment to
        # a running instance -- nothing has just come back from being down.
        return 0.0
    grace_s = max(startup_grace_s, _MIN_SETTLE_S)
    return max(0.0, entry["ready_at"] + grace_s - now)
