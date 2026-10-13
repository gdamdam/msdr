"""Decoder backends: subprocess wrapper + line parsers for multimon-ng/minimodem.

One ProcessBackend per decoder id (one multimon-ng invocation per id, per the
registry, rather than one multimon-ng running every demodulator at once --
keeps failures and stderr isolated per decoder).
"""

import asyncio
import collections
import contextlib
import importlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from msdr.decoder.capability_matrix import MATRIX
from msdr.decoder.pcm import wav_stream_header
from msdr.decoder.registry import DecoderSpec

logger = logging.getLogger(__name__)

# 1s of mono s16 audio @ 22,050 Hz (input_rate for every P1 decoder) -- feed()
# drops rather than blocks once the backend's stdin can't keep up by more
# than this much, so a stuck child never backs up the whole pump loop.
_MAX_QUEUED_BYTES = 22050 * 2


Integrity = Literal["passed", "failed", "unknown", "unavailable", "not_applicable"]


class LoopEventQueue(asyncio.Queue):
    """asyncio.Queue for decoder events whose put_nowait() also works from a
    worker thread: IQDecoderSession runs backend.feed() off the event loop
    (asyncio.to_thread), and feed() is where in-process decoders emit events.
    asyncio.Queue itself isn't thread-safe, so off-loop puts are handed to the
    owning loop with call_soon_threadsafe. A bounded queue still raises
    QueueFull to the caller when it's already full."""

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        try:
            self._owner: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._owner = None  # built outside a loop (sync tests): plain queue

    def put_nowait(self, item) -> None:
        try:
            on_owner = asyncio.get_running_loop() is self._owner
        except RuntimeError:
            on_owner = False
        if on_owner or self._owner is None:
            super().put_nowait(item)
            return
        if self.full():
            raise asyncio.QueueFull
        self._owner.call_soon_threadsafe(self._put_from_thread, item)

    def _put_from_thread(self, item) -> None:
        # full() was checked in the worker thread; a race can only overfill
        # by a frame or two, which is dropped like any other QueueFull.
        with contextlib.suppress(asyncio.QueueFull):
            super().put_nowait(item)


class DecoderError(Exception):
    """Raised when a decoder can't be started (missing binary, bad backend id)."""


@dataclass(frozen=True)
class DecodeEvent:
    ts: float
    decoder: str
    kind: str
    text: str
    fields: dict[str, object]
    frequency_hz: int | None = None
    signal_id: str | None = None
    # image (wefax): path of the PNG this event's row data was/will be written
    # to (repos.DecodeEventRepo.add already stores it via getattr).
    image_path: str | None = None
    # image (wefax): set on the *finishing* event instead of a fresh
    # image_path -- tells the manager "merge me into the stored event whose
    # image_path equals this" (repo.update_event) rather than adding a new
    # row. The PyDecoder doesn't know its own stored event id, only the path
    # it was given at start, so the manager keeps that mapping (see
    # DecoderManager._drain_events).
    update_of_path: str | None = None
    # integrity/provenance. `integrity` is set from the decoder's
    # capability-matrix policy plus the backend's own check result (see
    # policy_integrity) -- never guessed; "unknown" is the honest default.
    integrity: Integrity = "unknown"
    integrity_note: str = ""
    source_id: str | None = None  # "live:sdrconnect" | "replay:<name>"
    generation: int | None = None  # source generation the input belonged to
    sample_index: int | None = None  # run-local sample index of the input block (replay)
    analysis_run_id: str | None = None  # replay/re-analysis run, so live counts aren't inflated
    run_index: int | None = None  # replay timeline run containing the input


def policy_integrity(decoder_id: str, backend: str) -> dict[str, str]:
    """integrity/integrity_note kwargs for an event from a line-oriented
    backend, derived only from MATRIX[decoder_id].integrity_policy.

    fcs/crc: the backend prints a frame only after its own check passed, so
    a line existing *is* the pass. none_exposed: the backend gives no flag.
    parity/fec/repeat_fields need a per-message result the caller has --
    without one this says "unknown", not "passed".
    """
    entry = MATRIX.get(decoder_id)
    if entry is None:
        return {"integrity": "unknown", "integrity_note": "no capability-matrix entry"}
    policy = entry.integrity_policy
    if policy == "fcs":
        return {"integrity": "passed", "integrity_note": f"AX.25 FCS verified by {backend}"}
    if policy == "crc":
        return {"integrity": "passed", "integrity_note": f"CRC verified by {backend}"}
    if policy == "builtin_check":
        return {"integrity": "passed", "integrity_note": f"{backend} built-in consistency check"}
    if policy == "none_exposed":
        return {"integrity": "unavailable", "integrity_note": f"{backend} exposes no validity flag"}
    return {
        "integrity": "unknown",
        "integrity_note": f"{policy}: no per-message result from {backend}",
    }


class PyDecoder(Protocol):
    """In-process decoder run by PythonBackend (backend="python", args=("module:Class",)).

    feed()/flush() run inside asyncio.to_thread -- not called concurrently
    with each other, so implementations don't need their own locking.
    """

    input_rate: int

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        """samples: float32 mono at input_rate. Returns 0..n events."""
        ...

    def flush(self) -> list[DecodeEvent]:
        """Called once, when the backend is stopped."""
        ...


async def reap_cancelled(task: asyncio.Task) -> None:
    """Await a task we just cancelled, swallowing *its* CancelledError only.

    A bare `suppress(CancelledError)` here would also eat a cancellation aimed
    at the caller (e.g. the manager's state consumer being stopped while it is
    itself inside stop_decoding), leaving that caller running forever.
    """
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


class ProcessBackend:
    """Runs one decoder subprocess; feed() pushes audio in, events() yields decodes out."""

    def __init__(
        self,
        argv: list[str],
        parser: Callable[[str], "DecodeEvent | None"],
        preamble: bytes = b"",
        decoder_id: str = "",
        merge_stderr: bool = False,
        event_log_path: Path | None = None,
        cleanup_empty_event_log: bool = False,
    ):
        self._argv = argv
        self._parser = parser
        self._preamble = preamble
        self.decoder_id = decoder_id
        # dsd-fme (measured, live) writes its entire console report -- Sync
        # lines included -- to stderr, not stdout; merge_stderr routes that
        # combined stream through the normal stdout line-parser path instead.
        self._merge_stderr = merge_stderr
        self._event_log_path = event_log_path
        self._cleanup_empty_event_log = cleanup_empty_event_log
        self._event_log_task: asyncio.Task | None = None
        self._event_log_offset = 0
        self._event_log_inode: int | None = None
        self._event_log_pending = b""

        self._proc: asyncio.subprocess.Process | None = None
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._writer_task: asyncio.Task | None = None

        self._write_buf: collections.deque[bytes] = collections.deque()
        self._queued_bytes = 0
        self._write_ready = asyncio.Event()
        self.dropped_frames = 0

        self._last_stderr_line: str | None = None

    async def start(self) -> None:
        # dsd-fme appends call metadata to -J rather than always printing it
        # on the console. Skip older sessions already present in that file.
        if self._event_log_path is not None:
            try:
                stat = self._event_log_path.stat()
            except FileNotFoundError:
                self._event_log_offset = 0
                self._event_log_inode = None
            else:
                self._event_log_offset = stat.st_size
                self._event_log_inode = stat.st_ino
            self._event_log_pending = b""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if self._merge_stderr else asyncio.subprocess.PIPE,
            )
        except BaseException:
            # The factory pre-creates this file (mkstemp) before we ever
            # run, and stop() -- the normal cleanup -- is a no-op without a
            # process, so a failed spawn would leak it.
            if self._cleanup_empty_event_log and self._event_log_path is not None:
                with contextlib.suppress(OSError):
                    self._event_log_path.unlink()
            raise
        self._stdout_task = asyncio.create_task(self._read_stdout())
        if not self._merge_stderr:
            self._stderr_task = asyncio.create_task(self._read_stderr())
        self._writer_task = asyncio.create_task(self._writer_loop())
        if self._event_log_path is not None:
            self._event_log_task = asyncio.create_task(self._read_event_log())
        if self._preamble:
            self.feed(self._preamble)

    def feed(self, pcm_s16: bytes) -> None:
        if self._queued_bytes + len(pcm_s16) > _MAX_QUEUED_BYTES:
            self.dropped_frames += 1
            return
        self._write_buf.append(pcm_s16)
        self._queued_bytes += len(pcm_s16)
        self._write_ready.set()

    def push_event(self, event: DecodeEvent) -> None:
        """Used by backends (e.g. minimodem's idle-flush) that produce events
        outside the normal stdout-line path."""
        self._events.put_nowait(event)

    async def _writer_loop(self) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            while True:
                await self._write_ready.wait()
                while self._write_buf:
                    data = self._write_buf.popleft()
                    self._queued_bytes -= len(data)
                    try:
                        self._proc.stdin.write(data)
                        await self._proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError, RuntimeError):
                        return
                self._write_ready.clear()
        except asyncio.CancelledError:
            raise

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                event = self._parser(line)
                if event is not None:
                    self._events.put_nowait(event)
        except asyncio.CancelledError:
            raise

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for raw in self._proc.stderr:
                self._last_stderr_line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        except asyncio.CancelledError:
            raise

    def _read_event_log_available(self) -> None:
        path = self._event_log_path
        assert path is not None
        try:
            stat = path.stat()
            if stat.st_ino != self._event_log_inode or stat.st_size < self._event_log_offset:
                self._event_log_inode = stat.st_ino
                self._event_log_offset = 0
                self._event_log_pending = b""
            with path.open("rb") as stream:
                stream.seek(self._event_log_offset)
                data = stream.read()
                self._event_log_offset = stream.tell()
        except FileNotFoundError:
            self._event_log_inode = None
            self._event_log_offset = 0
            self._event_log_pending = b""
            return
        except OSError as exc:
            logger.warning("Could not read %s event log: %s", self.decoder_id, exc)
            return
        if not data:
            return
        self._event_log_pending += data
        *lines, self._event_log_pending = self._event_log_pending.split(b"\n")
        for raw in lines:
            event = self._parser(raw.decode("utf-8", errors="replace").rstrip("\r"))
            if event is not None:
                self._events.put_nowait(event)

    async def _read_event_log(self) -> None:
        try:
            while True:
                self._read_event_log_available()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            self._read_event_log_available()
            raise

    def events(self) -> AsyncIterator[DecodeEvent]:
        async def _gen():
            while True:
                yield await self._events.get()

        return _gen()

    async def stop(self) -> None:
        if self._proc is None:
            return
        for task in (self._writer_task, self._stdout_task, self._stderr_task, self._event_log_task):
            if task is not None:
                task.cancel()
        for task in (self._writer_task, self._stdout_task, self._stderr_task, self._event_log_task):
            if task is not None:
                await reap_cancelled(task)
        if self._proc.stdin is not None:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()
        if self._cleanup_empty_event_log and self._event_log_path is not None:
            try:
                path = self._event_log_path
                if path.stat().st_size <= 256:
                    lines = path.read_bytes().splitlines()
                    if all(
                        b"DSD-FME Started and Event History Initialized" in line
                        or b"Any decoded voice calls or data calls display here" in line
                        for line in lines
                    ):
                        path.unlink()
            except OSError as exc:
                logger.warning("Could not clean %s event log: %s", self.decoder_id, exc)

    @property
    def error(self) -> str | None:
        """Last stderr line, but only once the process has actually exited --
        stderr chatter from a still-running decoder isn't an error."""
        if (
            self._proc is not None
            and self._proc.returncode is not None
            and self._proc.returncode != 0
        ):
            return self._last_stderr_line
        return None


class PythonBackend:
    """Runs a PyDecoder in-process (no subprocess) behind ProcessBackend's own
    surface, so DecodeChannel/manager need no changes to use either kind.

    feed() batches incoming s16 audio and, once >=100ms has accumulated,
    hands one float32 batch to a single background task that awaits
    `asyncio.to_thread(decoder.feed, samples)` -- heavy numpy work runs off
    the event loop, and batches are processed strictly one at a time (a
    PyDecoder keeps state across calls and isn't expected to be thread-safe
    against itself).
    """

    _BATCH_S = 0.1

    def __init__(self, decoder: PyDecoder, decoder_id: str):
        self._decoder = decoder
        self.decoder_id = decoder_id
        self._batch_bytes = max(2, int(decoder.input_rate * self._BATCH_S)) * 2  # s16 mono
        self._pending = bytearray()
        self._batch_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._error: str | None = None
        self._queued_bytes = 0
        self.dropped_frames = 0

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    def feed(self, pcm_s16: bytes) -> None:
        # A crashed worker never drains the queue; stop queueing (the failure
        # is already surfaced via .error) instead of growing without bound.
        if self._error is not None:
            return
        self._pending.extend(pcm_s16)
        if len(self._pending) >= self._batch_bytes:
            # Same cap as ProcessBackend: a decoder slower than realtime drops
            # audio rather than building an unbounded backlog.
            if self._queued_bytes + len(self._pending) > _MAX_QUEUED_BYTES:
                self.dropped_frames += 1
            else:
                self._queued_bytes += len(self._pending)
                self._batch_queue.put_nowait(bytes(self._pending))
            self._pending = bytearray()

    def events(self) -> AsyncIterator[DecodeEvent]:
        async def _gen():
            while True:
                yield await self._events.get()

        return _gen()

    async def _worker(self) -> None:
        try:
            while True:
                batch = await self._batch_queue.get()
                if batch is None:
                    try:
                        final_events = await asyncio.to_thread(self._decoder.flush)
                    except Exception as exc:  # noqa: BLE001 -- surfaced via .error, not raised
                        logger.exception("python decoder %s flush crashed", self.decoder_id)
                        self._error = str(exc) or type(exc).__name__
                        return
                    for event in final_events:
                        self._events.put_nowait(event)
                    return
                self._queued_bytes -= len(batch)
                try:
                    # Truncate to whole s16 samples: an odd byte count would
                    # make frombuffer raise and silently kill the worker.
                    even = batch[: len(batch) & ~1]
                    samples = np.frombuffer(even, dtype="<i2").astype(np.float32) / 32768.0
                    new_events = await asyncio.to_thread(self._decoder.feed, samples)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("python decoder %s crashed", self.decoder_id)
                    self._error = str(exc) or type(exc).__name__
                    return
                for event in new_events:
                    self._events.put_nowait(event)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        if not self._worker_task.done():
            if self._pending and self._error is None:
                self._batch_queue.put_nowait(bytes(self._pending))
            self._pending = bytearray()
            self._batch_queue.put_nowait(None)  # sentinel: worker flushes then returns
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except TimeoutError:
                self._worker_task.cancel()
                await reap_cancelled(self._worker_task)
        self._worker_task = None

    @property
    def error(self) -> str | None:
        return self._error


# Fields that count as a payload even when `text` is empty -- an address/tgt/src
# means the page carried an identifiable target, not just a bare tone/idle
# line (see _has_payload).
_PAYLOAD_FIELDS = ("address", "tgt", "src")


def _has_payload(text: str, fields: dict[str, object]) -> bool:
    if text:
        return True
    return any(fields.get(k) not in (None, "") for k in _PAYLOAD_FIELDS)


def parse_multimon_json(line: str, decoder_id: str) -> DecodeEvent | None:
    """One JSON object per line (POCSAG/FLEX/EAS/DTMF, --json).

    text = alpha | numeric | message | ""; fields = the object minus that key,
    with demod_name renamed to "demod".
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    text_key = next((k for k in ("alpha", "numeric", "message") if k in obj), None)
    text = obj.get(text_key, "") if text_key else ""
    fields = {k: v for k, v in obj.items() if k not in (text_key, "demod_name")}
    demod = obj.get("demod_name")
    if demod is not None:
        fields["demod"] = demod
    if not _has_payload(text, fields):
        return None
    return DecodeEvent(
        ts=time.time(),
        decoder=decoder_id,
        kind="text",
        text=text,
        fields=fields,
        **policy_integrity(decoder_id, "multimon-ng"),
    )


#  SRC>DEST[,PATH]:payload -- AX.25/TNC2 monitor-format addressing, shared by
# multimon-ng's -A plain lines and direwolf's monitor output. PATH may itself
# contain commas (digipeater list), so it's captured up to the final colon.
_TNC2_RE = re.compile(r"^([^>]+)>([^,:]+)(?:,([^:]+))?:(.*)$")


def _parse_tnc2(text: str) -> dict[str, object] | None:
    match = _TNC2_RE.match(text)
    if not match:
        return None
    src, dest, path, _payload = match.groups()
    return {"from": src.strip(), "to": dest.strip(), "path": path.split(",") if path else []}


class MorseBackend(ProcessBackend):
    """multimon MORSE_CW emits unprefixed characters, not framed lines."""

    def feed(self, pcm_s16: bytes) -> None:
        # SDRconnect's low-level CW audio can fall below multimon's adaptive
        # envelope threshold. Decoder-local +12 dB preserves keying gaps;
        # saturate rather than wrap int16. Speaker audio is unaffected.
        samples = np.frombuffer(pcm_s16, dtype="<i2").astype(np.int32)
        super().feed(np.clip(samples * 4, -32768, 32767).astype("<i2").tobytes())

    # Collect readable phrases across character-sized subprocess writes.
    phrase_idle_s = 2.0
    phrase_max_s = 8.0

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        pending = ""
        started = time.monotonic()
        while True:
            timeout = self.phrase_idle_s
            if pending:
                timeout = min(timeout, max(0.01, self.phrase_max_s - (time.monotonic() - started)))
            try:
                raw = await asyncio.wait_for(self._proc.stdout.read(256), timeout=timeout)
            except TimeoutError:
                raw = None
            if raw:
                if not pending:
                    started = time.monotonic()
                pending += raw.decode("ascii", errors="replace")
            # Whitespace between words is retained, not treated as a message.
            # The deadline prevents an uninterrupted transmission starving UI.
            if pending and (not raw or "\n" in pending or len(pending) >= 160
                            or time.monotonic() - started >= self.phrase_max_s):
                event = parse_multimon_plain("MORSE_CW: " + " ".join(pending.split()), "cw")
                pending = ""
                if event is not None:
                    self.push_event(event)
            if raw == b"":
                return


def parse_multimon_plain(line: str, decoder_id: str) -> DecodeEvent | None:
    """ "NAME: text" lines (APRS/CW, no --json) -> text, fields={"demod": NAME}.

    For AX.25/TNC2 payloads (APRS, packet9600) `text` also matches
    SRC>DEST[,PATH]:payload, so those get parsed into from/to/path fields too.
    """
    if ":" not in line:
        return None
    name, _, text = line.partition(":")
    name = name.strip()
    if not name:
        return None
    text = text.strip()
    fields: dict[str, object] = {"demod": name}
    tnc2 = _parse_tnc2(text)
    if tnc2 is not None:
        fields.update(tnc2)
    if not _has_payload(text, fields):
        return None
    return DecodeEvent(
        ts=time.time(),
        decoder=decoder_id,
        kind="text",
        text=text,
        fields=fields,
        **policy_integrity(decoder_id, "multimon-ng"),
    )


# direwolf monitor-format line: "[chan.subchan] SRC>DEST,PATH:payload". The
# subchannel is a number (e.g. "0.3"); a locally-transmitted frame instead
# prints "[0L]" and is not receive data, so it's excluded by requiring a dot.
_DIREWOLF_LINE_RE = re.compile(r"^\[\d+\.\d+\]\s+(.*)$")


def parse_direwolf_line(line: str, decoder_id: str) -> DecodeEvent | None:
    """direwolf monitor output -> TNC2 fields, ignoring chatter/tx-echo lines."""
    line = line.rstrip()
    if not line:
        return None
    match = _DIREWOLF_LINE_RE.match(line)
    if not match:
        return None
    payload_line = match.group(1)
    tnc2 = _parse_tnc2(payload_line)
    if tnc2 is None:
        return None
    fields = {**tnc2, "demod": "direwolf"}
    return DecodeEvent(
        ts=time.time(),
        decoder=decoder_id,
        kind="text",
        text=payload_line,
        fields=fields,
        **policy_integrity(decoder_id, "direwolf"),
    )


class LineCoalescer:
    """minimodem streams individual characters; this groups them into one
    DecodeEvent per newline, or after `idle_s` of no new characters.

    flush() is called by DecodeChannel.feed() at audio-frame cadence (~20 Hz
    -- see channel.py), which is fine-grained enough for a 2s idle check
    without a dedicated per-channel timer task.
    """

    def __init__(
        self, decoder_id: str, idle_s: float = 2.0, clock: Callable[[], float] = time.time
    ):
        self.decoder_id = decoder_id
        self.idle_s = idle_s
        self._clock = clock
        self._buf: list[str] = []
        self._last_char_at: float | None = None

    def feed_line(self, line: str) -> DecodeEvent | None:
        """Adapter for ProcessBackend's line-based stdout reader: a line
        boundary is itself a newline, so feed the chars then force an emit."""
        for ch in line:
            self.feed_char(ch)
        return self._emit()

    def feed_char(self, ch: str) -> DecodeEvent | None:
        if ch == "\n":
            return self._emit()
        self._buf.append(ch)
        self._last_char_at = self._clock()
        return None

    def flush(self) -> DecodeEvent | None:
        if (
            self._buf
            and self._last_char_at is not None
            and (self._clock() - self._last_char_at) >= self.idle_s
        ):
            return self._emit()
        return None

    def _emit(self) -> DecodeEvent | None:
        text = "".join(self._buf).strip()
        self._buf = []
        self._last_char_at = None
        if not text:
            return None
        return DecodeEvent(
            ts=time.time(),
            decoder=self.decoder_id,
            kind="text",
            text=text,
            fields={},
            **policy_integrity(self.decoder_id, "minimodem"),
        )


# -- dsd-fme (DMR/P25/NXDN digital voice) --------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# dsd-fme's startup banner uses box-drawing / block characters for its ASCII
# art logo -- never useful, so dropped before any other pattern gets a look.
# Range U+2500-U+259F (Box Drawing + Block Elements), not two literal characters.
_BANNER_RE = re.compile(r"[─-▟]")
_SYNC_RE = re.compile(r"Sync:\s*\+?(?P<protocol>[A-Za-z0-9]+)")
_CC_RE = re.compile(r"Color\s*Code\s*[=:]?\s*([0-9A-Fa-fx]+)", re.IGNORECASE)
_NAC_RE = re.compile(r"\bNAC\s*[=:]?\s*([0-9A-Fa-fx]+)", re.IGNORECASE)
_RAN_RE = re.compile(r"\bRAN\s*[=:]?\s*([0-9A-Fa-fx]+)", re.IGNORECASE)
_CAN_RE = re.compile(r"\bCAN\s*[=:]?\s*([0-9A-Fa-fx]+)", re.IGNORECASE)
_DPMR_CC_RE = re.compile(r"\bDPMR_CC_(\d+)\b", re.IGNORECASE)
_SLOT_RE = re.compile(r"\[?slot\s*(\d)\]?", re.IGNORECASE)
# Tolerant TGT/SRC matchers (plan): DMR prints "TGT=123 SRC=456", P25-style
# output prints "TG 123 SRC 456" -- "TGT?" accepts both "TG" and "TGT".
_TGT_RE = re.compile(r"(?:TGT?|DST)\s*[=:]?\s*([A-Za-z0-9_*#/-]+)", re.IGNORECASE)
_SRC_RE = re.compile(r"SRC\s*[=:]?\s*([A-Za-z0-9_*#/-]+)", re.IGNORECASE)
_DSDFME_METADATA_RE = re.compile(
    r"\b(?P<name>RPT1|RPT2|URCALL|MYCALL|UPLINK|DOWNLINK|DNL|UPL|DST|SRC|CAN|TG)"
    r"\s*[:=]\s*(?P<value>[A-Za-z0-9_*#/-]+)",
    re.IGNORECASE,
)
_ENC_RE = re.compile(r"\b(ENC|Encrypted|ALG)\b", re.IGNORECASE)
_NO_SYNC_RE = re.compile(r"\bNo\s*Sync\b", re.IGNORECASE)
_DSDFME_ERROR_RE = re.compile(r"\b(?:CRC|FEC)\s+ERR\b", re.IGNORECASE)
_CAP_PLUS_STATUS_RE = re.compile(
    r"\bCapacity Plus Channel Status - FL:\s*(\d+) TS:\s*(\d+) RS:\s*(\d+)"
    r" - Rest LSN:\s*(\d+) - (Single|Multi) Block\b",
    re.IGNORECASE,
)
_CAP_PLUS_LSN_RE = re.compile(r"\bLSN\s+(\d+):\s*([^;]+)", re.IGNORECASE)
_CAP_PLUS_SITE_RE = re.compile(
    r"\bSLCO Capacity Plus Site:\s*(\d+) - Rest LSN:\s*(\d+) - RS:\s*(\d+)",
    re.IGNORECASE,
)

# gap between TGT/SRC-bearing lines beyond which the same TGT/SRC pair is
# treated as a new call rather than a continuation (plan: "> 3 s gap").
_CALL_GAP_S = 3.0

_PROTOCOL_FIELD = {
    "DMR": "color_code",
    "P25": "nac",
    "NXDN": "ran",
    "NXDN48": "ran",
    "NXDN96": "ran",
    "IDAS": "ran",
    "DPMR": "color_code",
    "M17": "can",
}
_DSDFME_EXTRA_IDS = frozenset({"dstar", "ysf", "dpmr", "m17"})
_PROTOCOL_LABEL = {
    "DSTAR": "D-STAR",
    "YSF": "Yaesu System Fusion (YSF)",
    "DPMR": "dPMR",
    "M17": "M17",
}
_METADATA_FIELD_NAMES = {
    "RPT1": "repeater_1",
    "RPT2": "repeater_2",
    "URCALL": "destination",
    "MYCALL": "source",
    "UPLINK": "uplink",
    "UPL": "uplink",
    "DOWNLINK": "downlink",
    "DNL": "downlink",
    "DST": "destination",
    "TG": "talkgroup",
    "TGT": "talkgroup",
    "SRC": "source",
    "CAN": "can",
}


def _protocol_field(protocol: str) -> str:
    protocol = protocol.upper()
    if protocol in _PROTOCOL_FIELD:
        return _PROTOCOL_FIELD[protocol]
    if protocol.startswith("NXDN"):
        return "ran"
    if protocol.startswith("P25"):
        return "nac"
    return "color_code"


@dataclass
class _ActiveCall:
    fields: dict
    started_at: float
    last_seen_at: float


@dataclass
class DsdState:
    """Per-decoder parse state carried across parse_dsdfme_line() calls (one
    instance per running dsd-fme backend, like LineCoalescer's per-backend
    buffer)."""

    protocol: str | None = None
    colour: str | None = None
    slot: int | None = None
    # Active calls keyed by TDMA slot (None for single-channel protocols).
    # DMR carries two independent calls at once; tracking only one made every
    # line from the other slot "replace" it, shredding both calls into
    # sub-second fragments and attaching the WAV to the wrong fragment.
    calls: dict = field(default_factory=dict)
    # Not part of the plan's documented DsdState shape, but needed so the
    # pure line-parser can find the per-call WAV without a second parameter:
    # the backend sets this once, from the same calls_dir it passes to dsd-fme.
    calls_dir: Path | None = None
    # One line can produce several events (a slot's call ending plus the new
    # call starting, or "No Sync" ending both slots), but the parser returns
    # at most one per line -- the rest drain on the following lines.
    pending: collections.deque = field(default_factory=collections.deque)
    # True after a "Sync:" line, False after "No Sync", None until
    # dsd-fme has said either -- the only backend that reports sync at all.
    synchronized: bool | None = None
    # Optional D-STAR/YSF/dPMR/M17 identifiers reported in human-readable
    # dsd-fme lines; stored only when explicitly present in the line.
    metadata: dict = field(default_factory=dict)
    # Capacity Plus control messages arrive as a header followed by an LSN
    # status line. Keep only the latest observed state, so repeated CSBK
    # bursts and alternating TS values do not flood decoder results.
    cap_plus_pending: dict | None = None
    cap_plus_status: tuple | None = None
    cap_plus_site: tuple | None = None
    cap_plus_color: str | None = None
    cap_plus_burst_valid: bool = False

    @property
    def current(self) -> dict | None:
        """Most recently started active call's fields (any slot), or None."""
        if not self.calls:
            return None
        return max(self.calls.values(), key=lambda call: call.started_at).fields

    @property
    def started_at(self) -> float | None:
        if not self.calls:
            return None
        return max(call.started_at for call in self.calls.values())


def _id_key(value: str) -> str:
    # The -J event log zero-pads ids ("00000070") while stderr does not
    # ("70"); compare numerically so both spellings are the same call.
    return str(int(value)) if value.isdecimal() else value


# dsd-fme -P names per-call files "..._TGT_<tg>_SRC_<src>.wav"; audio it has
# no call metadata for lands in "19691231_..._TGT_0_SRC_0.wav" placeholders.
_CALL_WAV_IDS_RE = re.compile(r"_TGT_([^_]+)_SRC_([^_.]+)\.wav$")


def _find_call_wav(calls_dir: Path | None, since: float, tgt: str, src: str) -> str | None:
    """Newest *.wav under calls_dir whose filename carries this call's TGT/SRC
    and whose mtime >= since, or None. Matching by name rather than "newest
    file" keeps other slots, other band channels sharing calls_dir, and the
    TGT_0_SRC_0 placeholders from being linked to this call."""
    if calls_dir is None:
        return None
    want = (_id_key(tgt), _id_key(src))
    candidates = []
    try:
        for path in calls_dir.glob("*.wav"):
            match = _CALL_WAV_IDS_RE.search(path.name)
            if match is None or (_id_key(match.group(1)), _id_key(match.group(2))) != want:
                continue
            mtime = path.stat().st_mtime
            if mtime >= since:
                candidates.append((mtime, path))
    except OSError:
        return None
    if not candidates:
        return None
    return str(max(candidates)[1])


def _call_text(protocol: str, tgt: str, src: str, encrypted: bool) -> str:
    text = f"{protocol} TG {tgt} from {src}"
    if encrypted:
        text += " · encrypted"
    return text


def _start_call_fields(protocol: str, tgt: str, src: str, state: DsdState, encrypted: bool) -> dict:
    return {
        "protocol": protocol,
        "tgt": tgt,
        "src": src,
        _protocol_field(protocol): state.colour,
        "slot": state.slot,
        "encrypted": encrypted,
        "wav": None,
    }


def parse_dsdfme_line(line: str, decoder_id: str, state: DsdState) -> DecodeEvent | None:
    """Stateful dsd-fme stdout line parser -- see plan "Contracts" for the
    documented shape (banner/Sync/TGT-SRC/call-end handling)."""
    event = _parse_dsdfme_line(line, decoder_id, state)
    if event is not None:
        state.pending.append(event)
    return state.pending.popleft() if state.pending else None


def _start_call(
    state: DsdState, decoder_id: str, slot: int | None, fields: dict, now: float
) -> DecodeEvent:
    state.calls[slot] = _ActiveCall(fields=fields, started_at=now, last_seen_at=now)
    return DecodeEvent(
        ts=now,
        decoder=decoder_id,
        kind="call",
        text=_call_text(fields["protocol"], fields["tgt"], fields["src"], fields["encrypted"]),
        fields=dict(fields),
        **policy_integrity(decoder_id, "dsd-fme"),
    )


def _parse_dsdfme_line(line: str, decoder_id: str, state: DsdState) -> DecodeEvent | None:
    line = _ANSI_RE.sub("", line).strip()
    if not line or _BANNER_RE.search(line):
        return None

    now = time.time()

    # An error report is not a decoded control message. In particular, do
    # not attach a following LSN status line to a rejected CSBK header.
    if _DSDFME_ERROR_RE.search(line):
        state.cap_plus_pending = None
        if "CSBK" in line.upper():
            state.cap_plus_burst_valid = False
        return None

    if decoder_id == "dmr" and state.cap_plus_burst_valid:
        status_match = _CAP_PLUS_STATUS_RE.search(line)
        if status_match:
            fl, ts, rs, rest_lsn = map(int, status_match.group(1, 2, 3, 4))
            state.cap_plus_pending = {
                "fl": fl,
                "ts": ts,
                "rs": rs,
                "rest_lsn": rest_lsn,
                "block_type": status_match.group(5).lower(),
            }
            return None

        if state.cap_plus_pending is not None:
            lsn_matches = _CAP_PLUS_LSN_RE.findall(line)
            pending = state.cap_plus_pending
            state.cap_plus_pending = None
            if lsn_matches:
                lsn_status = {str(int(lsn)): value.strip() for lsn, value in lsn_matches}
                signature = (pending["rest_lsn"], tuple(sorted(lsn_status.items())))
                if signature != state.cap_plus_status:
                    state.cap_plus_status = signature
                    fields = {
                        "protocol": "DMR",
                        "system": "Capacity Plus",
                        "message_type": "channel_status",
                        "color_code": state.colour,
                        "rest_lsn": pending["rest_lsn"],
                        "lsn_status": lsn_status,
                        "fl": pending["fl"],
                        "ts": pending["ts"],
                        "rs": pending["rs"],
                        "block_type": pending["block_type"],
                    }
                    if state.cap_plus_site is not None:
                        fields["site"] = state.cap_plus_site[0]
                    return DecodeEvent(
                        ts=now,
                        decoder=decoder_id,
                        kind="control",
                        text=f"DMR Capacity Plus rest LSN {pending['rest_lsn']}: "
                        + ", ".join(
                            f"LSN {lsn} {value}"
                            for lsn, value in sorted(
                                lsn_status.items(), key=lambda pair: int(pair[0])
                            )
                        ),
                        fields=fields,
                        **policy_integrity(decoder_id, "dsd-fme"),
                    )

        site_match = _CAP_PLUS_SITE_RE.search(line)
        if site_match:
            site, rest_lsn, rs = map(int, site_match.groups())
            signature = (site, rest_lsn, rs)
            if signature == state.cap_plus_site:
                return None
            state.cap_plus_site = signature
            return DecodeEvent(
                ts=now,
                decoder=decoder_id,
                kind="control",
                text=f"DMR Capacity Plus site {site}, rest LSN {rest_lsn}",
                fields={
                    "protocol": "DMR",
                    "system": "Capacity Plus",
                    "message_type": "site_status",
                    "color_code": state.colour,
                    "site": site,
                    "rest_lsn": rest_lsn,
                    "rs": rs,
                },
                **policy_integrity(decoder_id, "dsd-fme"),
            )

    if _NO_SYNC_RE.search(line):
        state.synchronized = False
        state.cap_plus_pending = None
        state.cap_plus_burst_valid = False
        state.cap_plus_site = None
        state.colour = None
        state.slot = None
        state.protocol = None
        # Losing sync ends every slot's call; the wrapper drains state.pending.
        for slot in list(state.calls):
            state.pending.append(_emit_call_end(state, decoder_id, slot, now))
        return None

    sync_match = _SYNC_RE.search(line)
    if sync_match:
        was_synchronized = state.synchronized
        state.synchronized = True
        state.protocol = sync_match.group("protocol").upper()
        value = None
        for pattern in (_CC_RE, _NAC_RE, _RAN_RE, _CAN_RE, _DPMR_CC_RE):
            match = pattern.search(line)
            if match:
                value = match.group(1)
                break
        if value is not None:
            if state.protocol == "DMR" and state.cap_plus_color not in (None, value):
                state.cap_plus_status = None
                state.cap_plus_site = None
            if state.protocol == "DMR":
                state.cap_plus_color = value
            state.colour = value
        state.cap_plus_burst_valid = state.protocol == "DMR" and "CSBK" in line.upper()
        slot_match = _SLOT_RE.search(line)
        if slot_match:
            state.slot = int(slot_match.group(1))
        if decoder_id in _DSDFME_EXTRA_IDS and not was_synchronized:
            label = _PROTOCOL_LABEL.get(state.protocol, state.protocol)
            fields = {"protocol": state.protocol, "synchronized": True, **state.metadata}
            protocol_field = _protocol_field(state.protocol)
            if protocol_field and state.colour is not None:
                fields[protocol_field] = state.colour
            return DecodeEvent(
                ts=now,
                decoder=decoder_id,
                kind="sync",
                text=f"{label} synchronization detected",
                fields=fields,
                **policy_integrity(decoder_id, "dsd-fme"),
            )
        return None

    tgt_match = _TGT_RE.search(line)
    src_match = _SRC_RE.search(line)
    if tgt_match and src_match:
        tgt, src = tgt_match.group(1), src_match.group(1)
        # dsd-fme also prints labels such as "Dst/TG" in status output.
        # The permissive matcher can read that label as TGT=/TG, SRC=0;
        # these are not call identifiers and must not become NXDN events.
        if decoder_id in {"dmr", "p25", "nxdn", "nxdn96"} and not (
            tgt.isdecimal() and src.isdecimal()
        ):
            return None
        encrypted = bool(_ENC_RE.search(line))
        protocol = state.protocol or decoder_id.upper()
        if decoder_id in {"nxdn", "nxdn96"}:
            ran_match = _RAN_RE.search(line)
            if ran_match:
                state.colour = ran_match.group(1)

        slot_match = _SLOT_RE.search(line)
        slot = int(slot_match.group(1)) if slot_match else state.slot
        key = (_id_key(tgt), _id_key(src))
        # Same TGT/SRC within the gap continues the call whichever slot it is
        # filed under: stderr lines inherit the last Sync's slot, which can
        # lag the event-log line's explicit "Slot N".
        for call in state.calls.values():
            same_call = (
                (_id_key(call.fields["tgt"]), _id_key(call.fields["src"])) == key
                and (now - call.last_seen_at) <= _CALL_GAP_S
            )
            if same_call:
                call.last_seen_at = now
                if encrypted and not call.fields["encrypted"]:
                    call.fields["encrypted"] = True
                return None

        fields = {**_start_call_fields(protocol, tgt, src, state, encrypted), **state.metadata}
        fields["slot"] = slot
        if slot in state.calls:
            # A different call replaced this slot's call without an explicit
            # "No Sync" line -- close the old one first.
            state.pending.append(_emit_call_end(state, decoder_id, slot, now))
        state.pending.append(_start_call(state, decoder_id, slot, fields, now))
        return None

    if decoder_id in _DSDFME_EXTRA_IDS:
        updates = {
            _METADATA_FIELD_NAMES[match.group("name").upper()]: match.group("value")
            for match in _DSDFME_METADATA_RE.finditer(line)
        }
        changed = {key: value for key, value in updates.items() if state.metadata.get(key) != value}
        if changed:
            state.metadata.update(changed)
            label = _PROTOCOL_LABEL.get(state.protocol or decoder_id.upper(), decoder_id.upper())
            if state.current is None and {"source", "destination"} <= state.metadata.keys():
                source = state.metadata["source"]
                destination = state.metadata["destination"]
                protocol = state.protocol or decoder_id.upper()
                fields = {
                    **_start_call_fields(protocol, destination, source, state, False),
                    **state.metadata,
                }
                return _start_call(state, decoder_id, None, fields, now)
            text = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in changed.items())
            return DecodeEvent(
                ts=now,
                decoder=decoder_id,
                kind="metadata",
                text=f"{label}: {text}",
                fields={"protocol": state.protocol or decoder_id.upper(), **state.metadata},
                **policy_integrity(decoder_id, "dsd-fme"),
            )

    return None


def _emit_call_end(state: DsdState, decoder_id: str, slot: int | None, now: float) -> DecodeEvent:
    call = state.calls.pop(slot)
    current = call.fields
    duration_s = max(0.0, now - call.started_at)
    wav = _find_call_wav(state.calls_dir, call.started_at, current["tgt"], current["src"])
    fields = {**current, "duration_s": duration_s, "wav": wav}
    return DecodeEvent(
        ts=now,
        decoder=decoder_id,
        kind="call",
        text=_call_text(current["protocol"], current["tgt"], current["src"], current["encrypted"])
        + f" ({duration_s:.1f}s)",
        fields=fields,
        **policy_integrity(decoder_id, "dsd-fme"),
    )


def _dsdfme_backend(
    spec: DecoderSpec, binaries: dict[str, str | None], calls_dir: Path
) -> ProcessBackend:
    path = binaries.get("dsd-fme")
    if not path:
        raise DecoderError(f"{spec.id}: required binary 'dsd-fme' not found")
    # Created lazily (only once a dsd-fme decoder actually starts), not at
    # manager/settings construction time.
    calls_dir.mkdir(parents=True, exist_ok=True)
    # Band decoding may run the same protocol on multiple channels at once.
    # Give each process its own -J file so calls cannot leak across channels.
    log_fd, log_name = tempfile.mkstemp(
        prefix=f"{spec.id}-", suffix="-events.log", dir=calls_dir
    )
    os.close(log_fd)
    event_log_path = Path(log_name)
    argv = [
        path,
        *spec.args,
        "-i",
        "-",
        "-o",
        "null",
        "-7",
        str(calls_dir),
        "-P",
        "-J",
        str(event_log_path),
    ]
    state = DsdState(calls_dir=calls_dir)
    parser = lambda line: parse_dsdfme_line(line, spec.id, state)  # noqa: E731
    # merge_stderr=True: dsd-fme's Sync/TGT/SRC lines are all on stderr (live
    # test), not stdout.
    backend = ProcessBackend(
        argv, parser, decoder_id=spec.id, merge_stderr=True,
        event_log_path=event_log_path,
        cleanup_empty_event_log=True,
    )
    # Exposed for introspection/tests -- most of dsd-fme's output (Sync
    # lines) updates this state without ever emitting a DecodeEvent, mirroring
    # `backend.idle_coalescer` for minimodem above.
    backend.dsd_state = state
    return backend


# Measured (direwolf 1.8.1): without a config file in cwd/home direwolf exits 1
# ("Could not open configuration file direwolf.conf") before reading any
# audio; with one it also opens KISS TCP 8001 + AGW 8000, so a second
# instance (single + band) would collide. This receive-only config turns both
# network ports off; audio/rate/baud still come from the command line.
_DIREWOLF_CONF = "# msdr receive-only decoder: no network client ports\nAGWPORT 0\nKISSPORT 0\n"


def _direwolf_conf_path() -> str:
    path = Path(tempfile.gettempdir()) / "msdr-direwolf.conf"
    try:
        if not path.exists() or path.read_text() != _DIREWOLF_CONF:
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(_DIREWOLF_CONF)
            tmp.replace(path)  # atomic: a concurrent start never reads half a file
    except OSError as exc:
        raise DecoderError(f"direwolf: cannot write its config file {path}: {exc}") from exc
    return str(path)


def _direwolf_backend(spec: DecoderSpec, path: str, tail_args: tuple[str, ...]) -> ProcessBackend:
    # -c: msdr's receive-only config (see _DIREWOLF_CONF);
    # -n 1 -r <rate> -b 16: one channel of raw s16 mono at the P1 input rate;
    # -t 0 -q hd: no colored output, no heard-line/description chatter;
    # trailing "-": read audio from stdin.
    argv = [path, "-c", _direwolf_conf_path()]
    argv += ["-n", "1", "-r", str(spec.input_rate), "-b", "16", "-t", "0", "-q", "hd"]
    argv.extend(tail_args)
    argv.append("-")
    parser = lambda line: parse_direwolf_line(line, spec.id)  # noqa: E731
    return ProcessBackend(argv, parser, decoder_id=spec.id)


def _python_backend(spec: DecoderSpec, fax_dir: Path | None) -> PythonBackend:
    module_path, sep, class_name = spec.args[0].partition(":")
    if not sep or not module_path or not class_name:
        raise DecoderError(f"{spec.id}: malformed python backend args[0] {spec.args[0]!r}")
    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        if spec.kind == "image":
            # Only wefax (image output) needs a directory to write into --
            # text/time PyDecoders take no constructor arguments.
            resolved_fax_dir = Path(fax_dir) if fax_dir is not None else Path("decoder/fax")
            decoder = cls(resolved_fax_dir)
        else:
            decoder = cls()
    except DecoderError:
        raise
    except Exception as exc:  # noqa: BLE001 -- e.g. NotImplementedError placeholders (phase 2)
        raise DecoderError(f"{spec.id}: {exc}") from exc
    return PythonBackend(decoder, spec.id)


def build_backend(
    spec: DecoderSpec,
    binaries: dict[str, str | None],
    calls_dir: Path | None = None,
    fax_dir: Path | None = None,
) -> ProcessBackend | PythonBackend:
    # A preferred alt backend (e.g. direwolf for AX.25 packet) wins whenever
    # its binary resolves, even if the spec's own backend is also available.
    if spec.alt_backend is not None:
        alt_name, alt_args = spec.alt_backend
        alt_path = binaries.get(alt_name)
        if alt_path:
            return _direwolf_backend(spec, alt_path, tuple(alt_args.split()))

    if spec.backend == "dsdfme":
        return _dsdfme_backend(spec, binaries, calls_dir or Path("decoder/calls"))

    if spec.backend == "freedv":
        from msdr.decoder.external_decoders import build_freedv_backend

        return build_freedv_backend(spec, binaries)

    if spec.backend == "python":
        return _python_backend(spec, fax_dir)

    if spec.backend == "direwolf":
        path = binaries.get("direwolf")
        if not path:
            raise DecoderError(f"{spec.id}: required binary 'direwolf' not found")
        return _direwolf_backend(spec, path, spec.args)

    resolved = []
    for name in spec.binaries:
        path = binaries.get(name)
        if not path:
            raise DecoderError(f"{spec.id}: required binary '{name}' not found")
        resolved.append(path)
    argv = [resolved[0], *spec.args]

    if spec.backend == "multimon":
        if spec.id == "cw":
            return MorseBackend(argv, lambda line: None, decoder_id=spec.id)
        parser: Callable[[str], DecodeEvent | None]
        if "--json" in spec.args:
            parser = lambda line: parse_multimon_json(line, spec.id)  # noqa: E731
        else:
            parser = lambda line: parse_multimon_plain(line, spec.id)  # noqa: E731
        return ProcessBackend(argv, parser, decoder_id=spec.id)

    if spec.backend == "minimodem":
        coalescer = LineCoalescer(spec.id)
        backend = ProcessBackend(
            argv,
            coalescer.feed_line,
            preamble=wav_stream_header(spec.input_rate),
            decoder_id=spec.id,
        )
        # DecodeChannel looks for this attribute to drive the idle flush.
        backend.idle_coalescer = coalescer
        return backend

    raise DecoderError(f"{spec.id}: unknown backend '{spec.backend}'")
