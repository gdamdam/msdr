"""`BleSupervisor`: run `python -m msdr.ble.capture` (backend "hci") or
`python -m msdr.ble.sniffle_capture` (backend "sniffle") and feed its
JSON-lines output into an `AdvertiserTable` (docs/ble.md "msdr side"). Same
shape and rules as `msdr.wifi.supervisor.WifiSupervisor`: the stdout reader
never blocks, the process is restarted with backoff only while the user
wants receiving, and terminal exit codes become health states instead of
retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from typing import Literal

from msdr.ble.observations import AdvertiserTable

logger = logging.getLogger(__name__)

QUEUE_MAXSIZE = 4096
STALE_AFTER_S = 10.0
RETRY_BACKOFFS_S = (1.0, 2.0, 5.0, 10.0)
MAX_RETRIES = 5
LIST_TIMEOUT_S = 10.0
_RATE_EMA_ALPHA = 0.5

#: What a Bluetooth 4.x dongle in passive mode covers (docs/ble.md).
COVERAGE_HCI = (
    "legacy advertising, 1M PHY, channels 37-39 controller-scheduled; "
    "no extended advertising, no scan responses"
)

#: Backward-compatible alias; several tests/callers import this name.
COVERAGE = COVERAGE_HCI

_PHY_LABEL = {"1m": "1M PHY", "2m": "2M PHY", "coded": "Coded PHY"}

HealthState = Literal[
    "dependency_missing",
    "no_device",
    "claimed",
    "no_le",
    "firmware_mismatch",
    "wrong_baud",
    "stopped",
    "starting",
    "receiving",
    "stale",
    "failed",
]

# Helper exit codes (docs/ble.md) that describe the machine, not a crash.
# 6/7 are sniffle_capture-only; they never occur for the hci backend.
_TERMINAL_EXITS: dict[int, HealthState] = {
    1: "no_device",
    2: "claimed",
    4: "dependency_missing",
    5: "no_le",
    6: "firmware_mismatch",
    7: "wrong_baud",
}

_READY_FIELDS = (
    "vid",
    "pid",
    "product",
    "manufacturer",
    "serial",
    "bus",
    "address",
    "hci_version",
    "hci_revision",
    "lmp_version",
    "lmp_subversion",
    "manufacturer_id",
    "manufacturer_name",
    "le_features",
    "scan_type",
    "scan_interval_ms",
    "scan_window_ms",
    "helper_version",
    # sniffle backend only:
    "firmware_version",
    "api_level",
    "port",
    "baud",
    "phy",
    "channel",
    "extended",
)


def default_helper_command(backend: str = "hci") -> list[str]:
    if backend == "sniffle":
        return [sys.executable, "-m", "msdr.ble.sniffle_capture"]
    return [sys.executable, "-m", "msdr.ble.capture"]


def _coverage_for(
    backend: str, phy: str | None, channel: str | None
) -> str:
    """The honest coverage sentence for the Setup card and status line
    (docs/ble.md): Sniffle covers only what it is configured for, so this
    states the exact PHY/channel, never "all"."""
    if backend != "sniffle":
        return COVERAGE_HCI
    phy_label = _PHY_LABEL.get(phy or "1m", "1M PHY")
    if channel == "hop":
        chan_label = "channels 37-39 (dwell hop)"
    else:
        chan_label = f"channel {channel or '37'} only"
    return (
        f"{phy_label} on {chan_label}; legacy and extended advertising "
        "(aux-channel chaining followed); no scan responses, no connection "
        "following"
    )


async def _reap_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


class BleSupervisor:
    def __init__(
        self,
        *,
        helper_command: Sequence[str] | None = None,
        usb_id: str | None = None,
        backend: Literal["hci", "sniffle"] = "hci",
        serial_port: str | None = None,
        baud: int | None = None,
        phy: str | None = None,
        channel: str | None = None,
        on_status: Callable[[dict], None] | None = None,
    ) -> None:
        self.backend = backend
        self.helper_command = (
            list(helper_command) if helper_command else default_helper_command(backend)
        )
        self.usb_id = usb_id
        self.serial_port = serial_port
        self.baud = baud
        self.phy = phy or "1m"
        self.channel = channel or "37"
        self.on_status = on_status
        # MinuteAccumulator (docs/history.md), set by the service; None = off.
        self.history = None
        self.table: AdvertiserTable | None = None

        self._process: asyncio.subprocess.Process | None = None
        self._zombies: list = []  # see _terminate_process
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._aggregator_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        # Same reason as WifiSupervisor: the spawn is awaited before the
        # process is recorded, so start/stop/retry must not interleave.
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

        self._wanted = False
        self._starting = False
        self._state_override: HealthState | None = None
        self.reports = 0
        self._ingest_error_logged = False
        self.drops = 0
        self.parse_errors = 0
        self.last_report_at: float | None = None
        self.last_error: str | None = None
        self.last_exit_code: int | None = None
        self.last_diagnostic: str | None = None
        self.controller: dict | None = None
        self.report_rate_hz = 0.0
        self.retry_attempt = 0
        self.retry_at: float | None = None
        self._reports_at_last_tick = 0

    # --- commands ----------------------------------------------------------

    def _capture_command(self) -> list[str]:
        cmd = list(self.helper_command)
        if self.backend == "sniffle":
            if self.serial_port:
                cmd += ["--port", self.serial_port]
            if self.baud:
                cmd += ["--baud", str(self.baud)]
            cmd += ["--phy", self.phy, "--channel", self.channel]
        elif self.usb_id:
            cmd += ["--usb-id", self.usb_id]
        return cmd

    def _display_command(self) -> str:
        return " ".join(self._capture_command())

    async def devices(self) -> dict:
        """`--list`: Bluetooth-class USB devices (hci backend) or CP210x
        serial ports (sniffle backend) and any error. Never raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.helper_command,
                "--list",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return {"devices": [], "error": f"failed to start helper: {exc}"}
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=LIST_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return {"devices": [], "error": "helper --list timed out"}
        devices = []
        for raw in stdout.decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    devices.append(json.loads(line))
        error = None
        if proc.returncode != 0:
            error = _last_error_message(stderr) or f"helper --list exited {proc.returncode}"
        return {"devices": devices, "error": error}

    # --- lifecycle -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start receiving. Idempotent while running; after a terminal state
        it relaunches cleanly."""
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.running:
            self._wanted = True
            return
        if self._aggregator_task is not None:
            await self._stop_locked()
        self._wanted = True
        self.retry_attempt = 0
        self.retry_at = None
        self.table = AdvertiserTable()
        self.table.history = self.history
        self.reports = 0
        self.drops = 0
        self.parse_errors = 0
        self.last_report_at = None
        self.report_rate_hz = 0.0
        self._reports_at_last_tick = 0
        self._aggregator_task = asyncio.create_task(self._aggregate())
        self._tick_task = asyncio.create_task(self._tick())
        await self._spawn()

    async def _spawn(self) -> None:
        self._state_override = None
        self.last_error = None
        self.last_exit_code = None
        self.last_diagnostic = None
        self.controller = None
        self._starting = True
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._capture_command(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._state_override = "dependency_missing"
            self.last_error = str(exc)
            self._starting = False
            self._publish()
            return
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._publish()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        self._wanted = False
        self._state_override = None
        self.retry_attempt = 0
        self.retry_at = None
        if self._retry_task is not None:
            self._retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retry_task
            self._retry_task = None
        await self._terminate_process()
        tasks = (self._stdout_task, self._stderr_task, self._aggregator_task, self._tick_task)
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                await _reap_cancelled(task)
        self._stdout_task = self._stderr_task = self._aggregator_task = self._tick_task = None
        self._starting = False
        self._publish()

    async def _terminate_process(self) -> None:
        await self._reap_zombies()
        proc, self._process = self._process, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        except asyncio.CancelledError:
            # Cancelled mid-wait (app shutdown cancelling a survey's stop):
            # the helper has been told to exit but nobody has reaped it, so
            # it would outlive the event loop. Kill it and let the next
            # stop()/start() reap it.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            self._zombies.append(proc)
            raise

    async def _reap_zombies(self) -> None:
        zombies, self._zombies = self._zombies, []
        for proc in zombies:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=3.0)

    # --- reader / aggregator ------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    self.parse_errors += 1
                    continue
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    self.drops += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the pipe going away mid-read
            logger.warning("ble: stdout reader failed: %s", exc)

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        stream = self._process.stderr
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    self.last_diagnostic = text
                    continue
                self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("ble: stderr reader failed: %s", exc)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "ready":
            self._starting = False
            self.controller = {k: event.get(k) for k in _READY_FIELDS}
            self.retry_attempt = 0
            self.retry_at = None
        elif kind == "error":
            self.last_error = event.get("message") or event.get("code") or "helper error"
        self._publish()

    async def _aggregate(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                self.reports += 1
                if self.table is not None:
                    try:
                        entry = self.table.ingest(item)
                    except Exception:
                        # One bad report must not end ingestion silently; count
                        # it and log only the first.
                        self.table.parse_errors += 1
                        if not self._ingest_error_logged:
                            self._ingest_error_logged = True
                            logger.exception("ble: report ingest failed (later ones counted only)")
                        continue
                    if entry is not None:
                        self.last_report_at = entry.last_seen
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                instant = self.reports - self._reports_at_last_tick
                self._reports_at_last_tick = self.reports
                self.report_rate_hz = (
                    _RATE_EMA_ALPHA * instant + (1 - _RATE_EMA_ALPHA) * self.report_rate_hz
                )
                if self._process is not None and self._process.returncode is not None:
                    await self._on_process_exited()
                self._publish()
        except asyncio.CancelledError:
            raise

    async def _on_process_exited(self) -> None:
        proc, self._process = self._process, None
        self.last_exit_code = proc.returncode if proc is not None else None
        self._starting = False
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                await _reap_cancelled(task)
        self._stdout_task = self._stderr_task = None
        if not self._wanted:
            return
        terminal = _TERMINAL_EXITS.get(self.last_exit_code or 0)
        if terminal is not None:
            self._state_override = terminal
            return
        if self.retry_attempt >= MAX_RETRIES:
            return  # "failed"
        backoff = RETRY_BACKOFFS_S[min(self.retry_attempt, len(RETRY_BACKOFFS_S) - 1)]
        self.retry_attempt += 1
        self.retry_at = time.time() + backoff
        self._retry_task = asyncio.create_task(self._retry_after(backoff))

    async def _retry_after(self, backoff: float) -> None:
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return
        self.retry_at = None
        async with self._lock:
            if self._wanted:  # re-checked under the lock: stop() may have won
                await self._spawn()

    # --- status --------------------------------------------------------------------

    def health_state(self) -> HealthState:
        if self._state_override is not None:
            return self._state_override
        if not self._wanted:
            return "stopped"
        if self._starting:
            return "starting"
        if not self.running:
            # Between a crash and the retry the receiver is coming back, not
            # failed; "failed" is reserved for retries being exhausted.
            return "failed" if self.retry_attempt >= MAX_RETRIES else "starting"
        if self.last_report_at is not None and time.time() - self.last_report_at > STALE_AFTER_S:
            return "stale"
        return "receiving"

    def status(self) -> dict:
        table = self.table
        return {
            "state": self.health_state(),
            "wanted": self._wanted,
            "reports": self.reports,
            "report_rate": round(self.report_rate_hz, 2),
            "drops": self.drops,
            "parse_errors": self.parse_errors + (table.parse_errors if table is not None else 0),
            "malformed": table.malformed if table is not None else 0,
            "evictions": table.evictions if table is not None else 0,
            "last_report_at": self.last_report_at,
            "last_error": self.last_error,
            "last_exit_code": self.last_exit_code,
            "last_diagnostic": self.last_diagnostic,
            "retry_attempt": self.retry_attempt,
            "retry_at": self.retry_at,
            "controller": self.controller,
            "helper": {"command": self._display_command()},
            "coverage": _coverage_for(self.backend, self.phy, self.channel),
            "advertiser_count": len(table) if table is not None else 0,
            "backend": self.backend,
            "phy": self.phy if self.backend == "sniffle" else None,
            "channel": self.channel if self.backend == "sniffle" else None,
        }

    def _publish(self) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(self.status())
        except Exception:
            logger.exception("ble: on_status callback failed")


def _last_error_message(stderr: bytes) -> str | None:
    message = None
    for raw in stderr.decode("utf-8", errors="replace").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            event = json.loads(raw)
            if isinstance(event, dict) and event.get("event") == "error":
                message = event.get("message")
    return message
