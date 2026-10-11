"""Standalone ADS-B adapter around FlightAware's ``dump1090-fa``.

The process accepts signed 16-bit complex samples at 2.4 MHz on stdin and
publishes BaseStation/SBS messages on a private loopback TCP port. msdr's IQ
stream is signed 16-bit I/Q; this backend recentres it and applies an
anti-alias FIR while resampling to dump1090's input rate.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import socket
import time
from collections import deque

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError, reap_cancelled

_TARGET_RATE = 2_400_000
_MAX_PENDING_BYTES = 8 * 1024 * 1024
_FIR_HALF_TAPS = 48
_FIR_BATCH_OUTPUTS = 4096
_SBS_FIELDS = (
    "message_type",
    "transmission_type",
    "session_id",
    "aircraft_id",
    "icao",
    "flight_id",
    "generated_date",
    "generated_time",
    "logged_date",
    "logged_time",
    "callsign",
    "altitude_ft",
    "ground_speed_kt",
    "track_deg",
    "latitude",
    "longitude",
    "vertical_rate_fpm",
    "squawk",
    "alert",
    "emergency",
    "spi",
    "on_ground",
)


def parse_sbs_line(line: str) -> DecodeEvent | None:
    """Parse one dump1090 BaseStation MSG line into a structured event."""
    columns = line.rstrip("\r\n").split(",")
    if len(columns) < len(_SBS_FIELDS) or columns[0] != "MSG":
        return None
    values = dict(zip(_SBS_FIELDS, columns, strict=False))
    address = values.get("icao", "").strip().upper()
    if not address:
        return None
    fields: dict[str, object] = {k: v for k, v in values.items() if v}
    for name in (
        "transmission_type",
        "session_id",
        "aircraft_id",
        "altitude_ft",
        "ground_speed_kt",
        "track_deg",
        "vertical_rate_fpm",
    ):
        if fields.get(name):
            try:
                fields[name] = int(fields[name])
            except (TypeError, ValueError):
                pass
    for name in ("latitude", "longitude"):
        if fields.get(name):
            try:
                fields[name] = float(fields[name])
            except (TypeError, ValueError):
                pass
    # SBS timestamps have no timezone; dump1090 emits local wall-clock time.
    # This adapter consumes a local live stream, so receipt time is unambiguous.
    # Keep original date/time fields above for inspection without interpreting
    # them as UTC (which made fresh Pacific-time positions seven hours old).
    timestamp = time.time()
    callsign = values.get("callsign", "").strip()
    summary = f"{address} {callsign}".strip()
    return DecodeEvent(
        ts=timestamp,
        decoder="adsb",
        kind="text",
        text=summary,
        fields=fields,
        integrity="unavailable",
        integrity_note="dump1090-fa BaseStation output does not expose per-message CRC status",
    )


class _StreamingIQResampler:
    """Centered windowed-sinc low-pass and rational-rate IQ resampler.

    The polyphase position is calculated as an integer numerator over the
    reduced input/output rate ratio, so the sampling clock cannot drift across
    input blocks. Only the FIR history and current input block are retained.
    """

    def __init__(self, sample_rate_hz: int, offset_hz: float):
        common = math.gcd(sample_rate_hz, _TARGET_RATE)
        self._step_num = sample_rate_hz // common
        self._step_den = _TARGET_RATE // common
        self._sample_rate = sample_rate_hz
        self._offset_hz = offset_hz
        self._half = _FIR_HALF_TAPS
        self._offsets = np.arange(-self._half, self._half + 1, dtype=np.int64)
        self._window = np.hamming(len(self._offsets)).astype(np.float64)
        # Keep the passband below the 1.2 MHz output Nyquist limit. The Hamming
        # window strongly attenuates frequencies that would alias on decimation.
        self._cutoff = 0.45 * _TARGET_RATE / sample_rate_hz
        # Cache the rational FIR phases instead of recalculating millions of
        # sinc kernels per second on the receiver event loop.
        self._phase_weights = None
        if self._step_den <= 4096:
            fractions = np.arange(self._step_den)[:, None] / self._step_den
            distance = self._offsets[None, :] - fractions
            weights = 2 * self._cutoff * np.sinc(2 * self._cutoff * distance)
            weights *= self._window[None, :]
            self._phase_weights = (weights / weights.sum(axis=1, keepdims=True)).astype(
                np.float32
            )
        self._buffer = np.empty(0, dtype=np.complex64)
        self._buffer_start = 0
        self._input_count = 0
        self._next_output = (self._half * self._step_den + self._step_num - 1) // self._step_num

    def reset(self) -> None:
        self._buffer = np.empty(0, dtype=np.complex64)
        self._buffer_start = 0
        self._input_count = 0
        self._next_output = (self._half * self._step_den + self._step_num - 1) // self._step_num

    def process(self, data: bytes) -> np.ndarray:
        raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
        if not len(raw):
            return np.empty(0, dtype=np.complex64)
        iq = ((raw[:, 0] + 1j * raw[:, 1]) / 32768.0).astype(np.complex64)
        start = self._input_count
        indices = np.arange(start, start + len(iq), dtype=np.float64)
        self._input_count += len(iq)
        if self._offset_hz:
            phase = np.remainder(indices * self._offset_hz / self._sample_rate, 1.0)
            iq *= np.exp(-2j * np.pi * phase).astype(np.complex64)
        if self._step_num == 1 and self._step_den == 1:
            # Input already at the target rate: nothing to resample.
            return iq
        if len(self._buffer):
            self._buffer = np.concatenate((self._buffer, iq))
        else:
            self._buffer = iq
            self._buffer_start = start

        # Last output whose right FIR wing is available. The strict upper
        # bound leaves the newest samples for the next call's filter history.
        safe_centers = self._input_count - self._half
        limit = (safe_centers * self._step_den + self._step_num - 1) // self._step_num
        chunks: list[np.ndarray] = []
        offsets = self._offsets
        while self._next_output < limit:
            stop = min(limit, self._next_output + _FIR_BATCH_OUTPUTS)
            out_numbers = np.arange(self._next_output, stop, dtype=np.int64)
            time_numerators = out_numbers * self._step_num
            centers = time_numerators // self._step_den
            fractions = (time_numerators % self._step_den) / self._step_den
            sample_indices = centers[:, None] + offsets[None, :]
            local_indices = sample_indices - self._buffer_start
            # Each fractional phase gets its own normalized, shifted FIR.
            if self._phase_weights is not None:
                weights = self._phase_weights[time_numerators % self._step_den]
            else:
                distance = offsets[None, :] - fractions[:, None]
                weights = 2.0 * self._cutoff * np.sinc(2.0 * self._cutoff * distance)
                weights *= self._window[None, :]
                weights = (weights / weights.sum(axis=1, keepdims=True)).astype(np.float32)
            filtered = np.einsum("ij,ij->i", self._buffer[local_indices], weights)
            chunks.append(filtered.astype(np.complex64, copy=False))
            self._next_output = stop

        # Discard samples that can no longer contribute to a future output.
        next_center = (self._next_output * self._step_num) // self._step_den
        keep_from = max(self._buffer_start, next_center - self._half)
        discard = keep_from - self._buffer_start
        if discard > 0:
            self._buffer = self._buffer[discard:].copy()
            self._buffer_start = keep_from
        if not chunks:
            return np.empty(0, dtype=np.complex64)
        return np.concatenate(chunks)

    def to_uc8(self, data: bytes) -> bytes:
        samples = self.process(data)
        if not len(samples):
            return b""
        packed = np.empty((len(samples), 2), dtype=np.uint8)
        packed[:, 0] = np.clip(np.rint(samples.real * 127.5 + 127.5), 0, 255).astype(np.uint8)
        packed[:, 1] = np.clip(np.rint(samples.imag * 127.5 + 127.5), 0, 255).astype(np.uint8)
        return packed.tobytes()

    def to_sc16(self, data: bytes) -> bytes:
        """Retain weak signals that would quantize away in unsigned 8-bit IQ."""
        samples = self.process(data)
        if not len(samples):
            return b""
        packed = np.empty((len(samples), 2), dtype="<i2")
        packed[:, 0] = np.clip(np.rint(samples.real * 32768.0), -32768, 32767).astype("<i2")
        packed[:, 1] = np.clip(np.rint(samples.imag * 32768.0), -32768, 32767).astype("<i2")
        return packed.tobytes()


class StandaloneADSBBackend:
    """A bounded IQ-to-dump1090-fa process adapter for one ADS-B frequency."""

    decoder_id = "adsb"

    def __init__(
        self, binary: str | None, *, sample_rate_hz: int, center_hz: float, frequency_hz: float
    ):
        self.binary = binary
        self.sample_rate_hz = int(sample_rate_hz)
        self.offset_hz = float(frequency_hz - center_hz)
        self.error: str | None = None
        self.dropped_frames = 0
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)  # crash detail for stop()
        self._writer_task: asyncio.Task | None = None
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()
        self._writes: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queued_bytes = 0
        self._resampler = _StreamingIQResampler(self.sample_rate_hz, self.offset_hz)
        self._resample_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._raw_queued_bytes = 0
        self._resample_task: asyncio.Task | None = None
        self._sbs_port = 0
        self._sbs_writer: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        if not self.binary:
            raise DecoderError("ADS-B requires the dump1090-fa command-line decoder")
        if self.sample_rate_hz < _TARGET_RATE:
            raise DecoderError("ADS-B requires an IQ sample rate of at least 2.4 MHz")
        if abs(self.offset_hz) + _TARGET_RATE / 2 > self.sample_rate_hz / 2:
            raise DecoderError("ADS-B channel falls outside the available IQ span")
        # Reserve a private local port for the BaseStation stream.
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            self._sbs_port = int(sock.getsockname()[1])
        finally:
            sock.close()
        self._process = await asyncio.create_subprocess_exec(
            self.binary,
            "--device-type",
            "ifile",
            "--ifile",
            "-",
            "--iformat",
            "SC16",
            "--net",
            "--net-bind-address",
            "127.0.0.1",
            "--net-ri-port",
            "0",
            "--net-ro-port",
            "0",
            "--net-bi-port",
            "0",
            "--net-bo-port",
            "0",
            "--net-sbs-port",
            str(self._sbs_port),
            "--quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Drain stderr from the start (a full pipe would stall dump1090-fa);
        # the tail is what stop() reports when the process exits non-zero.
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await self._connect_sbs()
        except BaseException:
            await self.stop()
            raise
        self._reader_task = asyncio.create_task(self._read_sbs())
        self._writer_task = asyncio.create_task(self._write_iq())
        self._resample_task = asyncio.create_task(self._resample_iq())

    async def _connect_sbs(self) -> None:
        deadline = asyncio.get_running_loop().time() + 8.0
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise DecoderError(
                    f"dump1090-fa exited during startup ({self._process.returncode})"
                )
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self._sbs_port)
                self._reader, self._sbs_writer = reader, writer
                return
            except (ConnectionRefusedError, OSError) as exc:
                last_error = exc
                await asyncio.sleep(0.05)
        raise DecoderError(f"dump1090-fa did not open its SBS output port: {last_error}")

    def feed(self, data: bytes) -> None:
        # The polyphase FIR resample is real DSP work; do it off the event
        # loop (_resample_iq, via asyncio.to_thread) rather than inline here.
        if not data or self.error:
            return
        if len(data) % 4:
            self.dropped_frames += 1
            return
        if self._raw_queued_bytes + len(data) > _MAX_PENDING_BYTES:
            self.dropped_frames += 1
            return
        self._raw_queued_bytes += len(data)
        self._resample_queue.put_nowait(data)

    async def _resample_iq(self) -> None:
        # A single consumer task processes blocks strictly in feed() order,
        # so output ordering is preserved even though the FIR itself runs in
        # a worker thread.
        try:
            while True:
                data = await self._resample_queue.get()
                self._raw_queued_bytes -= len(data)
                try:
                    converted = await asyncio.to_thread(self._resampler.to_sc16, data)
                except Exception as exc:
                    self.error = f"IQ resample failed: {exc}"
                    continue
                if not converted:
                    continue
                if self._queued_bytes + len(converted) > _MAX_PENDING_BYTES:
                    self.dropped_frames += 1
                    continue
                self._queued_bytes += len(converted)
                self._writes.put_nowait(converted)
        except asyncio.CancelledError:
            raise

    async def _write_iq(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            while True:
                data = await self._writes.get()
                if data is None:
                    return
                self._queued_bytes -= len(data)
                self._process.stdin.write(data)
                await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            self.error = f"dump1090 IQ input closed: {exc}"
        except asyncio.CancelledError:
            raise

    async def _read_sbs(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                event = parse_sbs_line(line.decode("ascii", errors="replace"))
                if event is not None:
                    self._events.put_nowait(event)
            self.error = "dump1090 SBS output closed"
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            self.error = str(exc)

    def events(self):
        async def _gen():
            while True:
                yield await self._events.get()

        return _gen()

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        async for raw in self._process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                self._stderr_tail.append(line)

    async def stop(self) -> None:
        for task in (self._reader_task, self._writer_task, self._resample_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._writer_task, self._resample_task):
            if task is not None:
                await reap_cancelled(task)
        self._reader_task = self._writer_task = self._resample_task = None
        if self._sbs_writer is not None:
            self._sbs_writer.close()
            with contextlib.suppress(Exception):
                await self._sbs_writer.wait_closed()
            self._sbs_writer = None
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
            if self._stderr_task is not None:
                # The process has exited, so stderr hits EOF; bound the wait anyway.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._stderr_task, timeout=1.0)
                self._stderr_task = None
            if self._process.returncode and not self.error:
                detail = "\n".join(self._stderr_tail)[-1000:].strip()
                self.error = f"dump1090-fa exited {self._process.returncode}"
                if detail:
                    self.error += f": {detail}"
            self._process = None
        self._reader = None
        self._resampler.reset()
        self._writes = asyncio.Queue()
        self._queued_bytes = 0
