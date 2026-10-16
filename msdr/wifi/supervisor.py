"""`WifiSupervisor`: start/stop the `msdr-wifi-capture` helper subprocess and
feed its output into a `NetworkTable` (and, while recording, a session's raw
pcap bytes). See docs/wifi.md ("msdr side") for the contract this implements.

This module never imports anything from `msdr.radio`, `msdr.api` or
`msdr.recorder` -- it has no notion of SDRconnect, receiver ownership, the
pre-roll recorder, the scanner, survey or tracking, and never touches any of
them. The helper is a separate process; this is only a reader of its pcap
stdout and JSON-lines stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from msdr.wifi import radiotap
from msdr.wifi.activity import TICK_CREDIT_CAP_S
from msdr.wifi.observations import NetworkTable
from msdr.wifi.pcap import PcapStreamReader, Record

logger = logging.getLogger(__name__)

DEFAULT_HELPER_PATH = "~/dev/radio/rtl8812au-helper/.venv/bin/msdr-wifi-capture"
HELPER_NAME = "msdr-wifi-capture"
# A helper checkout next to this msdr checkout (e.g. ~/dev/SDR/msdr and
# ~/dev/SDR/rtl8812au-helper). Meaningless for a site-packages install, where
# it simply never exists.
SIBLING_HELPER_PATH = (
    Path(__file__).resolve().parents[2].parent / "rtl8812au-helper" / ".venv" / "bin" / HELPER_NAME
)
REBUILD_COMMAND = (
    "rm -rf .venv && python3 -m venv .venv && "
    ".venv/bin/pip install -r upstream/requirements.txt -e upstream -e wrapper"
)


def find_helper(helper_path: str | None) -> Path | None:
    """The helper to run: settings.wifi.helper_path, then `msdr-wifi-capture`
    on PATH, then a sibling checkout of this msdr checkout, then the old
    default. First existing file wins; None when there is none."""
    candidates = [Path(helper_path).expanduser()] if helper_path else []
    on_path = shutil.which(HELPER_NAME)
    if on_path:
        candidates.append(Path(on_path))
    candidates += [SIBLING_HELPER_PATH, Path(DEFAULT_HELPER_PATH).expanduser()]
    return next((c for c in candidates if c.is_file()), None)


def helper_problem(path: Path) -> str | None:
    """Why an existing helper script can't run, or None. Catches the common
    case of a moved helper folder: a venv hard-codes its own location in each
    script's `#!` line, so the interpreter it names no longer exists and exec
    fails with a bare "No such file or directory"."""
    try:
        with path.open("rb") as fh:
            first = fh.readline(512).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        return f"cannot read helper {path}: {exc}"
    if not first.startswith("#!"):
        return None  # a binary, or no interpreter line to check
    interpreter = first[2:].strip().split(" ")[0]
    if interpreter.startswith("/") and not Path(interpreter).exists():
        root = path.parent.parent.parent  # <root>/.venv/bin/msdr-wifi-capture
        return (
            f"helper's Python venv is broken: {interpreter} no longer exists "
            f"(was the helper folder moved?). Rebuild it: cd {root} && {REBUILD_COMMAND}"
        )
    return None

#: Records buffered between the stdout reader and the aggregator task. The
#: reader always uses put_nowait and drops (counting) on overflow rather than
#: ever blocking -- the helper's stdout pipe must never back up.
QUEUE_MAXSIZE = 2048

#: No frame seen for this long while otherwise healthy -> "stale".
STALE_AFTER_S = 10.0

#: Retry backoff (seconds) after an unwanted process exit, while the caller
#: still wants receiving -- docs/wifi.md's "restart on crash only when the
#: user asked for receiving". 5 tries total (4 waits between them, indexed by
#: (attempt - 1) below); the 5th failure leaves state "failed" with no
#: further auto-retry until start() is called again.
RETRY_BACKOFFS_S = (1.0, 2.0, 5.0, 10.0)
MAX_RETRIES = 5

#: --list / devices() subprocess timeout.
LIST_TIMEOUT_S = 5.0

#: Frame-rate EMA smoothing (new_rate = alpha*instant + (1-alpha)*old).
_RATE_EMA_ALPHA = 0.5

HealthState = Literal[
    "unavailable",
    "dependency_missing",
    "no_device",
    "stopped",
    "starting",
    "receiving",
    "stale",
    "failed",
]

# Helper exit codes (docs/wifi.md): 0 ok, 1 open failed (no device found --
# there's only ever one adapter and no selector in this release), 2 bring-up
# failed, 3 USB error during capture.
_EXIT_NO_DEVICE = 1


@dataclass
class HelperReady:
    channel: int | None = None
    frequency_mhz: int | None = None
    helper_version: str | None = None
    upstream_commit: str | None = None
    # Passive-only guarantees (docs/wifi.md): the wrapper clears the MAC TX
    # engine bits after bring-up and reports what it found/left in REG_CR.
    host_tx_dma_disabled: bool | None = None
    mac_tx_enabled: bool | None = None
    reg_cr_before: str | None = None
    reg_cr_after: str | None = None


def _transmitter(rt, data: bytes) -> str | None:
    """Transmitter address (addr2) of a good-FCS management or data frame, else
    None. Control frames are skipped (ACK and CTS carry no addr2), and a bad
    FCS means the address itself may be corrupt. The address is only compared
    against the sensing source, never stored here."""
    if rt is None or isinstance(rt, radiotap.ParseError):
        return None
    if rt.flags is not None and rt.flags.bad_fcs:
        return None
    body = data[rt.header_length:]
    if len(body) < 16 or (body[0] >> 2) & 0x3 == 1:
        return None
    return body[10:16].hex(":")


def _raw_signal(rt) -> int | None:
    """Per-packet signal code for the activity heatmap, same vendor-first
    priority `NetworkTable._ingest` uses for `signal_history`: the msdr-raw
    `packet_power_code` when the helper wrote one, else the first vendor byte
    (older helpers), else radiotap's dBm antenna signal field. Never
    raises -- a ParseError or missing field both just mean no signal."""
    if rt is None or isinstance(rt, radiotap.ParseError):
        return None
    raw = radiotap.msdr_raw(rt)
    if raw is not None and raw.packet_power_code is not None:
        return raw.packet_power_code
    if rt.vendor:
        payload = rt.vendor[min(rt.vendor.keys())]
        if payload:
            return payload[0]
    return rt.dbm_antsignal


async def _reap_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


class WifiSupervisor:
    def __init__(
        self,
        *,
        helper_path: str | None = None,
        helper_command: Sequence[str] | None = None,
        on_status: Callable[[dict], None] | None = None,
    ) -> None:
        # `helper_command` (test-only, see settings.WifiSettings.helper_command)
        # is an explicit argv and wins over `helper_path` when both are given.
        self.helper_path = helper_path
        self.helper_command = list(helper_command) if helper_command else None
        self.on_status = on_status
        # MinuteAccumulator (docs/history.md), set by the service; None = off.
        self.history = None
        # ActivityAccumulator (docs/wifi.md "Wi-Fi activity"), set by the
        # service; None = off. `_activity_tick_ref` is the wall time of the
        # previous tick that counted as listening -- reset on every start()
        # (and so on every hop) so the starting/restart interval is never
        # credited as listened time.
        self.activity = None
        self._activity_tick_ref: float | None = None
        # SensingAccumulator (docs/wifi.md "Sensing"), set by the service from
        # wifi.sensing_source; None = off. It drops every other address itself.
        self.sensing = None
        # False for a sensing-only embedder (motionscope): no NetworkTable is built, so no
        # other address's BSSID or SSID is ever held. msdr's own service leaves it True.
        self.observe_networks = True

        self.channel: int | None = None
        self.table: NetworkTable | None = None
        self._record_sink: Callable[[Record], bool] | None = None
        self.on_recording_full: Callable[[], None] | None = None

        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._aggregator_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._zombies: list = []  # see _terminate_process
        # start()/stop()/retry all await the subprocess spawn before the
        # process is recorded; serializing them keeps a second start (or a
        # stop during a spawn) from orphaning the first one's process/tasks.
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[Record] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

        self._wanted = False
        self._starting = False
        self._state_override: HealthState | None = None  # unavailable/dependency_missing/no_device
        self.frames = 0
        self.drops = 0
        self.bad_fcs = 0
        self.parse_errors = 0
        self._ingest_error_logged = False
        self.oversize_dropped = 0
        self.last_frame_at: float | None = None
        self.last_error: str | None = None
        self.last_exit_code: int | None = None
        self.last_diagnostic: str | None = None
        self.ready: HelperReady | None = None
        self.frame_rate_hz = 0.0
        self.retry_attempt = 0
        self.retry_at: float | None = None
        self._frames_at_last_tick = 0

    # --- resolving the helper ------------------------------------------

    def _resolve_command(self, channel: int) -> list[str] | None:
        if self.helper_command:
            return [*self.helper_command, "--channel", str(channel)]
        resolved = find_helper(self.helper_path)
        if resolved is None:
            return None
        return [str(resolved), "--channel", str(channel)]

    def _helper_display_path(self) -> str:
        if self.helper_command:
            return " ".join(self.helper_command)
        found = find_helper(self.helper_path)
        return str(found or Path(self.helper_path or DEFAULT_HELPER_PATH).expanduser())

    def _resolve_list_command(self) -> list[str] | None:
        if self.helper_command:
            return [*self.helper_command, "--list"]
        resolved = find_helper(self.helper_path)
        if resolved is None:
            return None
        return [str(resolved), "--list"]

    def _helper_problem(self, command: list[str]) -> str | None:
        return None if self.helper_command else helper_problem(Path(command[0]))

    # --- public lifecycle ------------------------------------------------

    async def devices(self) -> dict:
        """Run `--list` and return its parsed devices plus any error. Never
        raises -- a missing/broken helper is reported in the result."""
        cmd = self._resolve_list_command()
        if cmd is None:
            return {"devices": [], "error": "helper not found"}
        problem = self._helper_problem(cmd)
        if problem:
            return {"devices": [], "error": problem}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
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
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                devices.append(json.loads(line))
        error = None
        if proc.returncode != 0 and not devices:
            error = stderr.decode("utf-8", errors="replace").strip() or (
                f"helper --list exited {proc.returncode}"
            )
        return {"devices": devices, "error": error}

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(
        self,
        channel: int,
        *,
        configured_channel: int | None = None,
        keep_table: bool = False,
    ) -> None:
        """Start (or restart on a new channel) receiving. Idempotent: calling
        start() again with the same channel while already running is a
        no-op; a different channel (or a prior terminal state) stops and
        relaunches cleanly.

        `keep_table=True` (the Wi-Fi channel survey, docs/history.md) keeps
        the existing NetworkTable -- and its `history` attribute -- across
        the hop instead of replacing it, so networks accumulate over the
        whole survey rather than being forgotten on every channel change;
        only `configured_channel` is updated."""
        # Cancellation-safe: the locked body runs as its own task. A caller
        # cancelled part-way (a survey hop stopped while its helper starts,
        # or app shutdown) would otherwise interrupt _stop_locked/_spawn and
        # leave a helper terminated-but-unreaped or launched-but-unregistered,
        # which then outlives the event loop. The lock is taken here, before
        # the body is scheduled, so a stop() issued right after the cancel
        # queues behind the body instead of running first and missing the
        # helper the body is about to launch.
        await self._lock.acquire()
        body = asyncio.ensure_future(
            self._start_body(channel, configured_channel=configured_channel, keep_table=keep_table)
        )
        await asyncio.shield(body)

    async def _start_body(
        self, channel: int, *, configured_channel: int | None, keep_table: bool
    ) -> None:
        """Runs with self._lock already held by start(); releases it."""
        try:
            await self._start_locked(
                channel, configured_channel=configured_channel, keep_table=keep_table
            )
        finally:
            self._lock.release()

    async def _start_locked(
        self, channel: int, *, configured_channel: int | None, keep_table: bool
    ) -> None:
        if self.running and self.channel == channel:
            self._wanted = True
            return
        if self.running or self._aggregator_task is not None:
            await self._stop_locked()
        self._wanted = True
        self.retry_attempt = 0
        self.retry_at = None
        self.channel = channel
        self._activity_tick_ref = None
        if not self.observe_networks:
            self.table = None
        elif keep_table and self.table is not None:
            self.table.configured_channel = configured_channel or channel
        else:
            self.table = NetworkTable(configured_channel=configured_channel or channel)
            self.table.history = self.history
        self.frames = 0
        self.drops = 0
        self.bad_fcs = 0
        self.parse_errors = 0
        self.oversize_dropped = 0
        self.last_frame_at = None
        self.frame_rate_hz = 0.0
        self._frames_at_last_tick = 0
        self._aggregator_task = asyncio.create_task(self._aggregate())
        self._tick_task = asyncio.create_task(self._tick())
        await self._spawn(channel)

    async def _spawn(self, channel: int) -> None:
        self._state_override = None
        self.last_error = None
        self.last_exit_code = None
        self.ready = None
        self.last_diagnostic = None
        command = self._resolve_command(channel)
        if command is None:
            self._state_override = "unavailable"
            self._publish()
            return
        problem = self._helper_problem(command)
        if problem:
            self._state_override = "dependency_missing"
            self.last_error = problem
            self._publish()
            return
        self._starting = True
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
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
        logger.debug("helper pid %s spawned for channel %s", self._process.pid, channel)
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._publish()

    async def stop(self) -> None:
        """Stop receiving: SIGTERM, wait up to 3 s, then SIGKILL. Idempotent
        and always cancels/reaps every task, even if the process was never
        running (e.g. start() failed with "unavailable")."""
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
        for task in (self._stdout_task, self._stderr_task, self._aggregator_task, self._tick_task):
            if task is not None:
                task.cancel()
        for task in (self._stdout_task, self._stderr_task, self._aggregator_task, self._tick_task):
            if task is not None:
                await _reap_cancelled(task)
        self._stdout_task = self._stderr_task = self._aggregator_task = self._tick_task = None
        self._starting = False
        self._publish()

    async def _terminate_process(self) -> None:
        await self._reap_zombies()
        proc, self._process = self._process, None
        logger.debug("terminating helper pid %s", proc.pid if proc else None)
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

    def attach_recording(
        self,
        sink: Callable[[Record], bool] | None,
        *,
        on_full: Callable[[], None] | None = None,
    ) -> None:
        """Set (or clear, with None) the active recording session's sink --
        every record ingested into the table is also handed to it (`sink`
        returns False once the session has hit its size/duration limit, at
        which point the sink is detached and `on_full` is called)."""
        self._record_sink = sink
        self.on_recording_full = on_full

    # --- reader/aggregator tasks ------------------------------------------

    def _enqueue(self, record: Record) -> None:
        """Hand one parsed pcap record to the aggregator -- never blocks
        (put_nowait): a full queue just counts the drop so the stdout reader
        can never stall behind a slow aggregator."""
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.drops += 1

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        reader = PcapStreamReader()
        stream = self._process.stdout
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                for record in reader.feed(chunk):
                    self._enqueue(record)
                self.oversize_dropped = reader.oversize_dropped
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the stdout pipe going away mid-read, etc.
            logger.warning("wifi: stdout reader failed: %s", exc)

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
                self._handle_stderr_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("wifi: stderr reader failed: %s", exc)

    def _handle_stderr_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "ready":
            self._starting = False
            self.ready = HelperReady(
                channel=event.get("channel"),
                frequency_mhz=event.get("frequency_mhz"),
                helper_version=event.get("helper_version"),
                upstream_commit=event.get("upstream_commit"),
                host_tx_dma_disabled=event.get("host_tx_dma_disabled"),
                mac_tx_enabled=event.get("mac_tx_enabled"),
                reg_cr_before=event.get("reg_cr_before"),
                reg_cr_after=event.get("reg_cr_after"),
            )
            self.retry_attempt = 0
            self.retry_at = None
        elif kind == "stats":
            pass  # frame counts are tracked from the pcap stream itself
        elif kind == "error":
            self.last_error = event.get("message") or event.get("code") or "helper error"
        elif kind == "stopped":
            pass
        self._publish()

    async def _aggregate(self) -> None:
        try:
            while True:
                record = await self._queue.get()
                self.frames += 1
                self.last_frame_at = record.ts
                try:
                    rt = None
                    if (self.table is not None or self.activity is not None
                            or self.sensing is not None):
                        rt = radiotap.parse(record.data)
                    if self.table is not None:
                        if isinstance(rt, radiotap.ParseError):
                            self.parse_errors += 1
                        else:
                            if rt.flags is not None and rt.flags.bad_fcs:
                                self.bad_fcs += 1
                            self.table.ingest(record, rt)
                    if self.activity is not None and self.channel is not None:
                        # Every received pcap record counts as a packet here
                        # (docs/wifi.md "Wi-Fi activity": "packets this adapter
                        # received", not just the beacons/probe-responses the
                        # NetworkTable aggregates) -- attributed to the channel
                        # the receiver was actually tuned to, never a frame's own
                        # DS Parameter Set channel.
                        self.activity.add_packet(record.ts, self.channel, _raw_signal(rt))
                    if self.sensing is not None:
                        tx = _transmitter(rt, record.data)
                        if tx is not None:
                            self.sensing.add(record.ts, tx, _raw_signal(rt))
                except Exception:
                    # One bad frame must not end ingestion while health still
                    # says "receiving"; count it and log only the first.
                    self.parse_errors += 1
                    if not self._ingest_error_logged:
                        self._ingest_error_logged = True
                        logger.exception("wifi: frame ingest failed (further errors counted only)")
                if self._record_sink is not None:
                    try:
                        keep_going = self._record_sink(record)
                    except Exception:
                        logger.exception("wifi: recording sink failed")
                        keep_going = True
                    if not keep_going:
                        on_full = self.on_recording_full
                        self._record_sink = None
                        self.on_recording_full = None
                        if on_full is not None:
                            try:
                                on_full()
                            except Exception:
                                logger.exception("wifi: on_recording_full callback failed")
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        """1 Hz: frame-rate EMA, stale detection, crash detection/retry, and
        the (<=1 Hz) status publish."""
        try:
            while True:
                await asyncio.sleep(1.0)
                instant = self.frames - self._frames_at_last_tick
                self._frames_at_last_tick = self.frames
                self.frame_rate_hz = (
                    _RATE_EMA_ALPHA * instant + (1 - _RATE_EMA_ALPHA) * self.frame_rate_hz
                )
                if self._process is not None and self._process.returncode is not None:
                    await self._on_process_exited()
                self._credit_activity_tick()
                self._publish()
        except asyncio.CancelledError:
            raise

    def _credit_activity_tick(self) -> None:
        """Called once per `_tick` (~1 Hz): credit the wall time since the
        previous counted tick to (second, channel) -- only while actually
        receiving on a known channel (docs/wifi.md "Wi-Fi activity"
        coverage rules). `start()` resets `_activity_tick_ref` to None on
        every hop/restart, and leaving the listening states here does the
        same, so the gap in between is never credited."""
        if self.activity is None:
            return
        listening = self.health_state() in ("receiving", "stale") and self.channel is not None
        if not listening:
            self._activity_tick_ref = None
            return
        now = time.time()
        prev, self._activity_tick_ref = self._activity_tick_ref, now
        if prev is None:
            return
        elapsed = min(now - prev, TICK_CREDIT_CAP_S)
        if elapsed > 0:
            self.activity.credit_listened(now, self.channel, elapsed)

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
        if self.last_exit_code == _EXIT_NO_DEVICE:
            # Pulling the adapter is reported as the helper exiting with an
            # error, not a crash to retry -- stay in no_device until the
            # caller explicitly starts or stops again.
            self._state_override = "no_device"
            return
        if self.retry_attempt >= MAX_RETRIES:
            return  # health_state() reports "failed": wanted, not running, no override
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
            # Re-checked under the lock: a stop() that won the race while
            # this slept must not be followed by a fresh spawn.
            if not self._wanted or self.channel is None:
                return
            await self._spawn(self.channel)

    # --- status -------------------------------------------------------

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
        if self.last_frame_at is not None and time.time() - self.last_frame_at > STALE_AFTER_S:
            return "stale"
        return "receiving"

    def status(self) -> dict:
        ready = self.ready
        # parse_errors folds in both layers: radiotap-level (self.parse_errors)
        # and dot11-level (table.parse_errors_global -- malformed/truncated
        # beacons and probe-responses only; "unsupported_frame", e.g. control
        # frames this parser never decodes, is not an error, see dot11.parse
        # and NetworkTable.ingest).
        table_parse_errors = self.table.parse_errors_global if self.table is not None else 0
        return {
            "state": self.health_state(),
            "wanted": self._wanted,
            "channel": self.channel,
            "frequency_mhz": ready.frequency_mhz if ready is not None else None,
            "frames": self.frames,
            "frame_rate": round(self.frame_rate_hz, 2),
            "drops": self.drops,
            "bad_fcs": self.bad_fcs,
            "parse_errors": self.parse_errors + table_parse_errors,
            "oversize_dropped": self.oversize_dropped,
            "evictions": self.table.evictions if self.table is not None else 0,
            "last_frame_at": self.last_frame_at,
            "last_error": self.last_error,
            "last_exit_code": self.last_exit_code,
            "last_diagnostic": self.last_diagnostic,
            "retry_attempt": self.retry_attempt,
            "retry_at": self.retry_at,
            "helper": {
                "path": self._helper_display_path(),
                "upstream_commit": ready.upstream_commit if ready is not None else None,
                "host_tx_dma_disabled": ready.host_tx_dma_disabled if ready is not None else None,
                "mac_tx_enabled": ready.mac_tx_enabled if ready is not None else None,
            },
            "network_count": len(self.table) if self.table is not None else 0,
        }

    def _publish(self) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(self.status())
        except Exception:
            logger.exception("wifi: on_status callback failed")
