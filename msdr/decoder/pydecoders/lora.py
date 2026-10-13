"""Receive-only LoRa PHY and mesh payload parsing for live/replayed SDR IQ.

`LoRaBackend` is msdr's own native numpy LoRa receiver (msdr/decoder/
pydecoders/lora_phy.py): no external dependency, built in. `GrLoRaBackend`
keeps the previous GNU Radio + upstream GPL-3.0-or-later ``gr-lora_sdr``
OOT-module path as an optional cross-check backend; GNU Radio is loaded
only when that backend starts. See docs/lora-native.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import math
import queue
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError, LoopEventQueue
from msdr.decoder.pydecoders.lora_phy import LoRaParams, LoRaReceiver
from msdr.decoder.pydecoders.lorawan import parse_lorawan

_MAX_SOURCE_CHUNKS = 24
_MAX_DECODE_EVENTS = 256
_MAX_FRAME_BYTES = 255
_MESHTASTIC_SYNC = 0x2B
_DEFAULT_SYNC = 0x12
_OS_FACTOR = 4  # receiver samples per chip (gr-lora_sdr's usual os_factor)
_CUTOFF_BW = 0.5  # resampler low-pass edge, as a fraction of the LoRa bandwidth


class _Resampler:
    """Bounded vectorised rational low-pass resampler (numpy only, no scipy
    dependency), with continuous phase across `process()` calls -- same
    octave-decimation + windowed-sinc-interpolation structure as rs41.py's
    `_StreamingResampler`, generalised here to an arbitrary target rate
    (that one is pinned to the RS41 channel rate, 57,600 Hz) since LoRa's
    bandwidth is a runtime choice (125/250/500 kHz)."""

    def __init__(self, rate: int, target_rate: int, offset: float, cutoff_hz: float | None = None):
        stages = 0
        while rate / (1 << (stages + 1)) >= target_rate:
            stages += 1
        g = math.gcd(rate, target_rate * (1 << stages))
        self.step, self.den = rate // g, target_rate * (1 << stages) // g
        self.rate, self.offset = rate, offset
        taps = np.arange(49) - 24
        h = 0.45 * np.sinc(0.45 * taps) * np.hamming(49)
        self.stage_taps = (h / h.sum()).astype(np.float32)
        self.stages = [[np.zeros(48, np.complex64), 0] for _ in range(stages)]
        self.half = 24
        self.offsets = np.arange(-self.half, self.half + 1)
        self.window = np.hamming(len(self.offsets))
        # Final low-pass edge in cycles per (post-halving) input sample. LoRa
        # passes the channel bandwidth rather than 0.45 x the output rate:
        # the receiver works oversampled and picks one sample per chip,
        # which does not filter, so the noise must already be band-limited.
        stage_rate = rate / (1 << stages)
        self.cutoff = cutoff_hz / stage_rate if cutoff_hz else 0.45 * self.den / self.step
        # The fractional phase of output k is (k * step mod den) / den: with
        # few distinct phases, build every interpolation kernel once.
        self.table = None
        if self.den <= 4096:
            frac = np.arange(self.den) / self.den
            self.table = self._kernels(frac).astype(np.float32)
        self.buf = np.empty(0, np.complex64)
        self.start = self.count = self.raw_count = 0
        self.next = (self.half * self.den + self.step - 1) // self.step

    def _kernels(self, frac: np.ndarray) -> np.ndarray:
        dist = self.offsets[None, :] - np.atleast_1d(frac)[:, None]
        w = 2 * self.cutoff * np.sinc(2 * self.cutoff * dist) * self.window
        return w / w.sum(axis=1, keepdims=True)

    def process(self, data: bytes) -> np.ndarray:
        raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
        x = ((raw[:, 0] + 1j * raw[:, 1]) / 32768).astype(np.complex64)
        if not len(x):
            return np.empty(0, np.complex64)
        n = np.arange(self.raw_count, self.raw_count + len(x), dtype=np.float64)
        self.raw_count += len(x)
        if self.offset:
            x *= np.exp(-2j * np.pi * np.remainder(n * self.offset / self.rate, 1)).astype(
                np.complex64
            )
        for stage in self.stages:
            if not len(x):
                break
            tail, count = stage
            joined = np.concatenate((tail, x))
            filtered = np.convolve(joined, self.stage_taps, mode="valid")
            x = filtered[count & 1 :: 2].astype(np.complex64)
            stage[0] = joined[-48:].copy()
            stage[1] = count + len(filtered)
        self.count += len(x)
        if len(self.buf):
            self.buf = np.concatenate((self.buf, x))
        else:
            self.buf, self.start = x, self.count - len(x)
        limit = ((self.count - self.half) * self.den + self.step - 1) // self.step
        outs = []
        while self.next < limit:
            end = min(limit, self.next + 2048)
            k = np.arange(self.next, end, dtype=np.int64)
            num = k * self.step
            centers = num // self.den
            phase = num % self.den
            idx = centers[:, None] + self.offsets[None, :] - self.start
            w = self.table[phase] if self.table is not None else self._kernels(phase / self.den)
            outs.append(
                np.einsum("ij,ij->i", self.buf[idx], w.astype(np.float32)).astype(np.complex64)
            )
            self.next = end
        keep = max(self.start, self.next * self.step // self.den - self.half)
        if keep > self.start:
            self.buf = self.buf[keep - self.start :].copy()
            self.start = keep
        return np.concatenate(outs) if outs else np.empty(0, np.complex64)


class LoRaBackend:
    """Native (pure numpy) LoRa receiver fed by SDRconnect's raw interleaved
    int16 IQ: frequency-shifts and low-pass resamples down to exactly
    `bandwidth_hz` samples/sec (1 sample/chip), then runs msdr's own
    `LoRaReceiver` (msdr/decoder/pydecoders/lora_phy.py). No external
    dependency -- `backend_name = "msdr-native"`. Same constructor
    signature, `start`/`feed`/`stop`/`events`, mesh parsing, queue bounds
    and dropped counters as before; only the PHY underneath changed."""

    decoder_id = "lora"
    backend_name = "msdr-native"

    def __init__(
        self,
        spec,
        *,
        sample_rate_hz: int,
        center_hz: float,
        frequency_hz: float,
        spreading_factor: int = 11,
        bandwidth_hz: int = 250_000,
        coding_rate: int = 1,
        protocol: str = "lora",
    ) -> None:
        self.spec = spec
        self.input_rate = int(sample_rate_hz)
        self.center_hz = float(center_hz)
        self.frequency_hz = float(frequency_hz)
        self.spreading_factor = int(spreading_factor)
        self.bandwidth_hz = int(bandwidth_hz)
        self.coding_rate = int(coding_rate)
        self.protocol = str(protocol)
        if self.spreading_factor not in range(7, 13):
            raise DecoderError("LoRa spreading factor must be between 7 and 12")
        if self.bandwidth_hz not in {62_500, 125_000, 250_000, 500_000}:
            raise DecoderError("LoRa bandwidth must be 62.5, 125, 250, or 500 kHz")
        if self.coding_rate not in range(1, 5):
            raise DecoderError("LoRa coding rate index must be between 1 and 4 (4/(4+n))")
        if self.protocol not in {"lora", "meshtastic", "meshcore"}:
            raise DecoderError("LoRa protocol must be lora, meshtastic, or meshcore")
        if self.input_rate < self.bandwidth_hz:
            raise DecoderError(f"LoRa needs IQ rate ≥ {self.bandwidth_hz} samples/s")
        offset_hz = self.frequency_hz - self.center_hz
        if abs(offset_hz) + self.bandwidth_hz / 2 > self.input_rate / 2:
            raise DecoderError("LoRa channel falls outside the available IQ span")
        self.sync_word = _MESHTASTIC_SYNC if self.protocol == "meshtastic" else _DEFAULT_SYNC
        self.error: str | None = None
        self._running = False
        self._events: asyncio.Queue[DecodeEvent | None] = LoopEventQueue(_MAX_DECODE_EVENTS)
        # gr-lora_sdr's frame_sync works at os_factor 4 so it can pick the
        # sample phase closest to the chip timing; at 1 sample/chip a
        # fractional timing offset costs several dB (see LoRaReceiver).
        self.os_factor = max(1, min(_OS_FACTOR, self.input_rate // self.bandwidth_hz))
        self._resampler = (
            _Resampler(
                self.input_rate,
                self.bandwidth_hz * self.os_factor,
                offset_hz,
                cutoff_hz=_CUTOFF_BW * self.bandwidth_hz if self.os_factor > 1 else None,
            )
            if self.input_rate != self.bandwidth_hz or offset_hz
            else None
        )
        self._receiver = LoRaReceiver(
            LoRaParams(
                sf=self.spreading_factor,
                bw=self.bandwidth_hz,
                sync_word=self.sync_word,
                center_freq_hz=self.frequency_hz or None,
            ),
            os_factor=self.os_factor,
        )
        self.dropped_input_chunks = 0
        self.dropped_events = 0

    async def start(self) -> None:
        self._running = True

    def feed(self, data: bytes) -> None:
        if not self._running or not data:
            return
        if len(data) % 4:
            self.error = "incomplete interleaved int16 IQ pair"
            return
        if self._resampler is not None:
            samples = self._resampler.process(data)
        else:
            raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
            samples = ((raw[:, 0] + 1j * raw[:, 1]) / 32768.0).astype(np.complex64)
        if not len(samples):
            return
        try:
            packets = self._receiver.feed(samples)
        except Exception as exc:
            # A malformed/edge-case input should drop this chunk's decode,
            # not crash the channel the rest of the band depends on.
            self.dropped_input_chunks += 1
            self.error = f"LoRa native receiver error: {exc}"
            return
        for packet in packets:
            self._accept_packet(packet)

    def _accept_packet(self, packet) -> None:
        if packet.crc_valid is not True:
            # No CRC in the frame (crc_valid is None) or a CRC mismatch:
            # never emitted, same policy as the gr-lora_sdr backend.
            return
        fields = decode_mesh_payload(packet.payload, self.protocol, self.sync_word)
        event = DecodeEvent(
            ts=time.time(),
            decoder="lora",
            kind="text",
            text=str(fields.pop("text", "")),
            fields={
                **fields,
                "phy": "LoRa",
                "spreading_factor": self.spreading_factor,
                "bandwidth_hz": self.bandwidth_hz,
                "coding_rate": f"4/{4 + packet.cr}",
                "sync_word": f"0x{self.sync_word:02x}",
                "frequency_hz": int(self.frequency_hz),
                "payload_hex": packet.payload.hex(),
                "phy_crc": "passed",
                "cfo_hz": round(packet.cfo_hz, 1),
                "source": "iq",
            },
            frequency_hz=int(self.frequency_hz),
            integrity="passed",
            integrity_note="LoRa payload CRC-16 verified (msdr native receiver)",
        )
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            self.error = "LoRa decoder output queue is full; decoded frames were dropped"

    async def events(self) -> AsyncIterator[DecodeEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def stop(self) -> None:
        self._running = False
        while True:
            try:
                self._events.put_nowait(None)
                break
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._events.get_nowait()


class _FrameAssembler:
    """Join tagged payload stream bytes while preserving packet boundaries."""

    def __init__(self) -> None:
        self.pending = bytearray()
        self.length: int | None = None
        self.crc_valid = False

    def feed(self, items: bytes, starts: dict[int, tuple[int, bool] | None]) -> list[bytes]:
        packets: list[bytes] = []

        def consume(begin: int, end: int) -> None:
            if self.length is None or begin >= end:
                return
            needed = self.length - len(self.pending)
            take = min(needed, end - begin)
            self.pending.extend(items[begin : begin + take])
            if len(self.pending) == self.length:
                if self.crc_valid:
                    packets.append(bytes(self.pending))
                self.pending.clear()
                self.length = None

        cursor = 0
        for offset, frame in sorted(starts.items()):
            consume(cursor, offset)
            if self.length is not None:
                # A new sync before completion invalidates this partial frame.
                self.pending.clear()
                self.length = None
            if frame is None:
                self.pending.clear()
            else:
                self.length, self.crc_valid = frame
            cursor = offset
        consume(cursor, len(items))
        return packets


class GrLoRaBackend:
    """GNU Radio + gr-lora_sdr LoRa receiver, kept as an optional cross-check
    behind `LoRaBackend` (the native receiver below). Fed by SDRconnect's
    raw interleaved int16 IQ."""

    decoder_id = "lora"
    backend_name = "gr-lora_sdr"

    def __init__(
        self,
        spec,
        *,
        sample_rate_hz: int,
        center_hz: float,
        frequency_hz: float,
        spreading_factor: int = 11,
        bandwidth_hz: int = 250_000,
        coding_rate: int = 1,
        protocol: str = "lora",
    ) -> None:
        self.spec = spec
        self.input_rate = int(sample_rate_hz)
        self.center_hz = float(center_hz)
        self.frequency_hz = float(frequency_hz)
        self.spreading_factor = int(spreading_factor)
        self.bandwidth_hz = int(bandwidth_hz)
        self.coding_rate = int(coding_rate)
        self.protocol = str(protocol)
        if self.spreading_factor not in range(7, 13):
            raise DecoderError("LoRa spreading factor must be between 7 and 12")
        if self.bandwidth_hz not in {62_500, 125_000, 250_000, 500_000}:
            raise DecoderError("LoRa bandwidth must be 62.5, 125, 250, or 500 kHz")
        if self.coding_rate not in range(1, 5):
            raise DecoderError("LoRa coding rate index must be between 1 and 4 (4/(4+n))")
        if self.protocol not in {"lora", "meshtastic", "meshcore"}:
            raise DecoderError("LoRa protocol must be lora, meshtastic, or meshcore")
        if self.input_rate < self.bandwidth_hz:
            raise DecoderError(f"LoRa needs IQ rate ≥ {self.bandwidth_hz} samples/s")
        if abs(self.frequency_hz - self.center_hz) + self.bandwidth_hz / 2 > self.input_rate / 2:
            raise DecoderError("LoRa channel falls outside the available IQ span")
        self.sync_word = _MESHTASTIC_SYNC if self.protocol == "meshtastic" else _DEFAULT_SYNC
        self.error: str | None = None
        self._running = False
        self._events: asyncio.Queue[DecodeEvent | None] = asyncio.Queue(_MAX_DECODE_EVENTS)
        self._source_queue: queue.Queue[np.ndarray | None] = queue.Queue(_MAX_SOURCE_CHUNKS)
        self._source = None
        self._sink = None
        self._flowgraph = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self.dropped_input_chunks = 0
        self.dropped_events = 0

    async def start(self) -> None:
        if not _gr_lora_available():
            raise DecoderError(_GR_CROSSCHECK_HINT)
        try:
            self._loop = asyncio.get_running_loop()
            self._stopping = False
            self._running = True
            await asyncio.to_thread(self._start_flowgraph)
        except DecoderError:
            self._running = False
            self._request_source_eos()
            await asyncio.to_thread(self._stop_flowgraph)
            raise
        except Exception as exc:
            self._running = False
            self._request_source_eos()
            await asyncio.to_thread(self._stop_flowgraph)
            raise DecoderError(f"Could not start GNU Radio LoRa receiver: {exc}") from exc

    def feed(self, data: bytes) -> None:
        if not self._running or not data:
            return
        if len(data) % 4:
            self.error = "incomplete interleaved int16 IQ pair"
            return
        raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
        samples = ((raw[:, 0] + 1j * raw[:, 1]) / 32768.0).astype(np.complex64)
        try:
            self._source_queue.put_nowait(samples)
        except queue.Full:
            self.dropped_input_chunks += 1
            self.error = "LoRa receiver fell behind the live IQ stream; input chunks were dropped"

    async def events(self) -> AsyncIterator[DecodeEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def _request_source_eos(self) -> None:
        self._stopping = True
        # Wake a scheduler blocked on input, even when its bounded queue is full.
        with contextlib.suppress(queue.Empty):
            while True:
                self._source_queue.get_nowait()
        with contextlib.suppress(queue.Full):
            self._source_queue.put_nowait(None)

    async def stop(self) -> None:
        self._running = False
        self._request_source_eos()
        await asyncio.to_thread(self._stop_flowgraph)
        while True:
            try:
                self._events.put_nowait(None)
                break
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._events.get_nowait()

    def _start_flowgraph(self) -> None:
        import pmt  # type: ignore[import-not-found]
        from gnuradio import blocks, filter, gr  # type: ignore[import-not-found]
        from gnuradio.lora_sdr.lora_sdr_lora_rx import (
            lora_sdr_lora_rx,  # type: ignore[import-not-found]
        )

        backend = self
        loop = self._loop
        if loop is None:
            raise DecoderError("LoRa receiver must be started from an asyncio event loop")

        class IQSource(gr.sync_block):
            def __init__(self) -> None:
                gr.sync_block.__init__(
                    self,
                    name="msdr LoRa IQ source",
                    in_sig=None,
                    out_sig=[np.complex64],
                )
                self._remaining: np.ndarray | None = None
                self._offset = 0

            def work(self, input_items, output_items):
                output = output_items[0]
                written = 0
                while written < len(output):
                    if self._remaining is None:
                        try:
                            self._remaining = backend._source_queue.get(timeout=0.1)
                        except queue.Empty:
                            if backend._stopping:
                                return -1
                            return 0
                        self._offset = 0
                        if self._remaining is None:
                            return -1
                    count = min(len(output) - written, len(self._remaining) - self._offset)
                    output[written : written + count] = self._remaining[
                        self._offset : self._offset + count
                    ]
                    written += count
                    self._offset += count
                    if self._offset == len(self._remaining):
                        self._remaining = None
                return written

        class PacketSink(gr.sync_block):
            def __init__(self) -> None:
                gr.sync_block.__init__(
                    self,
                    name="msdr CRC-gated LoRa packet sink",
                    in_sig=[np.uint8],
                    out_sig=None,
                )
                self._assembler = _FrameAssembler()

            def work(self, input_items, output_items):
                items = input_items[0]
                first_abs = self.nitems_read(0)
                # gr-lora_sdr's crc_verif_impl.cc attaches frame_info with
                # pay_len and crc_valid at the output frame offset only when
                # its byte stream is connected. See upstream lib/crc_verif_impl.cc.
                tags = self.get_tags_in_window(0, 0, len(items), pmt.intern("frame_info"))
                starts: dict[int, tuple[int, bool] | None] = {}
                for tag in tags:
                    metadata = _pmt_dict(pmt, tag.value)
                    length = metadata.get("pay_len")
                    crc_value = metadata.get("crc_valid")
                    offset = int(tag.offset) - first_abs
                    if not 0 <= offset < len(items):
                        continue
                    if (
                        isinstance(length, int)
                        and not isinstance(length, bool)
                        and 0 < length <= _MAX_FRAME_BYTES
                        and isinstance(crc_value, bool)
                    ):
                        starts[offset] = (length, crc_value)
                    else:
                        # Preserve the boundary even when its metadata is
                        # incomplete; never let old pending bytes inherit a
                        # later frame's CRC decision.
                        starts[offset] = None

                for packet in self._assembler.feed(bytes(items), starts):
                    loop.call_soon_threadsafe(backend._accept_packet, packet, True)
                return len(items)

        source = IQSource()
        flowgraph = gr.top_block("msdr LoRa receive")
        offset_hz = self.frequency_hz - self.center_hz
        rotator = blocks.rotator_cc(-2.0 * math.pi * offset_hz / self.input_rate)
        divisor = math.gcd(self.input_rate, self.bandwidth_hz)
        resampler = filter.rational_resampler_ccc(
            self.bandwidth_hz // divisor,
            self.input_rate // divisor,
        )
        receiver = lora_sdr_lora_rx(
            center_freq=int(self.frequency_hz),
            bw=self.bandwidth_hz,
            cr=self.coding_rate,
            has_crc=True,
            impl_head=False,
            pay_len=255,
            samp_rate=self.bandwidth_hz,
            sf=self.spreading_factor,
            sync_word=[self.sync_word],
            soft_decoding=True,
            ldro_mode=2,
            print_rx=[False, False],
        )
        sink = PacketSink()
        flowgraph.connect(source, rotator, resampler, receiver)
        flowgraph.connect(receiver, sink)
        self._source = source
        self._sink = sink
        self._flowgraph = flowgraph
        flowgraph.start()

    def _accept_packet(self, payload: bytes, crc_valid: bool) -> None:
        if not self._running or not crc_valid:
            return
        fields = decode_mesh_payload(payload, self.protocol, self.sync_word)
        event = DecodeEvent(
            ts=time.time(),
            decoder="lora",
            kind="text",
            text=str(fields.pop("text", "")),
            fields={
                **fields,
                "phy": "LoRa",
                "spreading_factor": self.spreading_factor,
                "bandwidth_hz": self.bandwidth_hz,
                "coding_rate": f"4/{4 + self.coding_rate}",
                "sync_word": f"0x{self.sync_word:02x}",
                "frequency_hz": int(self.frequency_hz),
                "payload_hex": payload.hex(),
                "phy_crc": "passed",
                "source": "iq",
            },
            frequency_hz=int(self.frequency_hz),
            integrity="passed",
            integrity_note="LoRa payload CRC-16 verified by gr-lora_sdr",
        )
        try:
            self._events.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            self.error = "LoRa decoder output queue is full; decoded frames were dropped"

    def _stop_flowgraph(self) -> None:
        flowgraph = self._flowgraph
        if flowgraph is not None:
            # Keep Python block wrappers alive until GNU Radio has stopped
            # calling work(). Releasing them before wait() can crash native
            # scheduler threads on macOS.
            with contextlib.suppress(Exception):
                flowgraph.wait()
        self._flowgraph = None
        self._source = None
        self._sink = None


_GR_CROSSCHECK_HINT = (
    "Optional gr-lora_sdr cross-check backend not found. Install GNU Radio 3.10 and "
    "the gr-lora_sdr OOT module in the same Python environment to enable it. Follow "
    "the upstream conda instructions at "
    "https://github.com/tapparelj/gr-lora_sdr#installation, then restart msdr."
)


def _gr_lora_available() -> bool:
    try:
        if importlib.util.find_spec("gnuradio") is None:
            return False
        return importlib.util.find_spec("gnuradio.lora_sdr.lora_sdr_lora_rx") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _pmt_dict(pmt: Any, value: Any) -> dict[str, Any]:
    """Convert the small frame_info PMT dictionary into ordinary Python values."""
    result: dict[str, Any] = {}
    try:
        pairs = pmt.to_python(value)
    except Exception:
        return result
    if isinstance(pairs, dict):
        for key, item in pairs.items():
            try:
                name = pmt.symbol_to_string(key)
            except Exception:
                name = str(key).split("'")[-2] if "'" in str(key) else str(key)
            with contextlib.suppress(Exception):
                item = pmt.to_python(item)
            if isinstance(item, (int, bool)):
                result[name] = item
            elif isinstance(item, np.integer):
                result[name] = int(item)
            elif isinstance(item, np.bool_):
                result[name] = bool(item)
    return result


def decode_mesh_payload(
    payload: bytes, protocol: str, sync_word: int | None = None
) -> dict[str, Any]:
    """Decode known outer packet metadata; never infer plaintext from ciphertext."""
    if protocol == "meshtastic":
        return _decode_meshtastic_packet(payload)
    if protocol == "meshcore":
        return _decode_meshcore_packet(payload)
    wan = parse_lorawan(payload, sync_word=sync_word)
    if wan is not None:
        # Header only; FRMPayload stays encrypted and the MIC is never checked.
        return {
            **wan,
            "protocol": "LoRaWAN",
            "packet_type": "lorawan",
            "opaque": True,
            "encrypted": True,
            "payload_bytes": len(payload),
            "text": f"LoRaWAN {wan['lorawan_summary']} ({len(payload)} bytes)",
            "provenance": "LoRaWAN header parsed; payload not decrypted, MIC not verified",
        }
    return {
        "protocol": "LoRa",
        "packet_type": "raw",
        "opaque": True,
        "encrypted": None,
        "payload_bytes": len(payload),
        "text": f"LoRa packet ({len(payload)} bytes)",
        "provenance": "LoRa PHY decoded; application payload left uninterpreted",
    }


def _decode_meshtastic_packet(payload: bytes) -> dict[str, Any]:
    if len(payload) < 16:
        return _opaque_fields("Meshtastic", payload, "packet shorter than 16-byte mesh header")
    to_id = int.from_bytes(payload[0:4], "little")
    from_id = int.from_bytes(payload[4:8], "little")
    packet_id = int.from_bytes(payload[8:12], "little")
    flags, channel_hash, next_hop, relay_node = payload[12:16]
    data = payload[16:]
    return {
        "protocol": "Meshtastic",
        "packet_type": "mesh_packet",
        "packet_id": f"0x{packet_id:08x}",
        "from": f"0x{from_id:08x}",
        "to": f"0x{to_id:08x}",
        "flags": flags,
        "hop_limit": flags & 0x07,
        "channel_hash": f"0x{channel_hash:02x}",
        "next_hop": next_hop,
        "relay_node": relay_node,
        "portnum": None,
        "opaque": True,
        "encrypted": None,
        "payload_bytes": len(data),
        "text": f"Meshtastic packet {packet_id:08x} ({len(data)} payload bytes; opaque)",
        "provenance": "Meshtastic LoRa header parsed; application payload left opaque",
    }


def _decode_meshcore_packet(payload: bytes) -> dict[str, Any]:
    if len(payload) < 2:
        return _opaque_fields("MeshCore", payload, "packet is shorter than header and path length")
    header = payload[0]
    version = header >> 6
    kind = (header >> 2) & 0x0F
    route = header & 0x03
    offset = 1
    transport_codes = None
    if route in {0, 3}:
        if len(payload) < 6:
            return _opaque_fields("MeshCore", payload, "truncated transport codes")
        transport_codes = payload[offset : offset + 4].hex()
        offset += 4
    if len(payload) <= offset:
        return _opaque_fields("MeshCore", payload, "missing path length")
    path_meta = payload[offset]
    offset += 1
    hops = path_meta & 0x3F
    hash_size = ((path_meta >> 6) & 0x03) + 1
    if hash_size == 4 or hops * hash_size > 64:
        return _opaque_fields("MeshCore", payload, "invalid path metadata")
    path_bytes = hops * hash_size
    if len(payload) < offset + path_bytes:
        return _opaque_fields("MeshCore", payload, "truncated path")
    path = payload[offset : offset + path_bytes]
    body = payload[offset + path_bytes :]
    names = {
        0: "request",
        1: "response",
        2: "text_message",
        3: "acknowledgment",
        4: "advertisement",
        5: "group_text",
        6: "group_data",
        7: "anonymous_request",
        8: "path",
        9: "trace",
        10: "multipart",
        11: "control",
        15: "raw_custom",
    }
    name = names.get(kind, "reserved")
    if version != 0:
        return _opaque_fields("MeshCore", payload, f"unsupported payload version {version + 1}")
    encrypted = True if kind == 15 else False if kind in {0, 1, 2, 3, 4, 7, 8, 9, 10, 11} else None
    return {
        "protocol": "MeshCore",
        "packet_type": name,
        "payload_version": version + 1,
        "route_type": route,
        "transport_codes_hex": transport_codes,
        "path_hops": hops,
        "path_hash_bytes": hash_size,
        "path_hex": path.hex(),
        "opaque": True,
        "encrypted": encrypted,
        "mesh_payload_hex": body.hex(),
        "payload_bytes": len(body),
        "text": f"MeshCore {name.replace('_', ' ')} ({len(body)} payload bytes)",
        "provenance": "MeshCore packet header parsed; application payload left opaque",
    }


def _opaque_fields(protocol: str, payload: bytes, reason: str) -> dict[str, Any]:
    return {
        "protocol": protocol,
        "packet_type": "malformed_or_truncated",
        "opaque": True,
        "encrypted": None,
        "payload_bytes": len(payload),
        "text": f"{protocol} payload ({len(payload)} bytes; header not parsed)",
        "provenance": reason,
    }
