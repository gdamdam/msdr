"""Standalone FT8 adapter around WSJT-X's `jt9` WAV-file decoder.

The CLI is a process dependency (not a Python decoder); no event is emitted
unless jt9 prints a decoded FT8 line for a complete 15-second input window.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import math
import re
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError, reap_cancelled

_WINDOW_S = 15
_AUDIO_RATE = 12_000
_DECODE_LINE = re.compile(
    r"^\s*(?P<utc>\d{4,6})\s+(?P<snr>-?\d+)\s+(?P<dt>-?\d+(?:\.\d+)?)\s+"
    r"(?P<audio_hz>\d+)\s+~\s+(?P<message>.+?)\s*$"
)


def parse_jt9_line(line: str, *, window_epoch: float) -> DecodeEvent | None:
    """Parse a decoded FT8 row from jt9 stdout, ignoring chatter/other modes."""
    match = _DECODE_LINE.match(line)
    if not match:
        return None
    message = match.group("message").strip()
    if not message or message.endswith((" JT9", " JT65", " QRA64", " MSK144")):
        return None
    utc = match.group("utc")
    fields: dict[str, object] = {
        "utc": utc,
        "snr_db": int(match.group("snr")),
        "time_offset_s": float(match.group("dt")),
        "audio_frequency_hz": int(match.group("audio_hz")),
        "mode": "FT8",
        "window_epoch": window_epoch,
    }
    return DecodeEvent(
        ts=window_epoch,
        decoder="ft8",
        kind="text",
        text=message,
        fields=fields,
        integrity="passed",
        integrity_note="WSJT-X jt9 FT8 decoder accepted the message's LDPC/CRC checks",
    )


class StandaloneFT8Backend:
    """Bounded 15-second IQ windower and asynchronous jt9 WAV decoder."""

    decoder_id = "ft8"

    def __init__(
        self, binary: str | None, *, sample_rate_hz: int, center_hz: float, frequency_hz: float
    ):
        self.binary = binary
        self.sample_rate_hz = int(sample_rate_hz)
        self.offset_hz = float(frequency_hz - center_hz)
        self.error: str | None = None
        self.dropped_windows = 0
        self._decimation = self.sample_rate_hz // _AUDIO_RATE
        self._taps = self._make_taps()
        self._tail = np.zeros(len(self._taps) - 1, np.complex64)
        self._input_count = 0
        self._iq_pending = bytearray()
        self._block_samples = max(4096, len(self._taps) - 1)
        self._fft_size = 1 << (self._block_samples + 2 * (len(self._taps) - 1) - 1).bit_length()
        self._filter_fft = np.fft.fft(self._taps, self._fft_size)
        self._window_epoch: float | None = None
        self._skip_audio = 0
        self._audio_pending = bytearray()
        self._window_bytes = _AUDIO_RATE * _WINDOW_S * 2
        self._windows: asyncio.Queue[tuple[bytes, float] | None] = asyncio.Queue(maxsize=1)
        self._events: asyncio.Queue[DecodeEvent] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._temp: tempfile.TemporaryDirectory | None = None

    async def start(self) -> None:
        if not self.binary:
            raise DecoderError("FT8 requires the WSJT-X jt9 command-line decoder")
        if self.sample_rate_hz <= _AUDIO_RATE or self.sample_rate_hz % _AUDIO_RATE:
            raise DecoderError(
                "FT8 requires an IQ sample rate that is an integer multiple of 12000 Hz"
            )
        if self.offset_hz < -self.sample_rate_hz / 2 or (
            self.offset_hz + 3_000 > self.sample_rate_hz / 2
        ):
            raise DecoderError("FT8 upper sideband falls outside the available IQ span")
        self._temp = tempfile.TemporaryDirectory(prefix="msdr-ft8-")
        self._worker = asyncio.create_task(self._decode_loop())

    def feed(self, data: bytes) -> None:
        self._feed(data, None)

    def feed_at(self, data: bytes, first_sample_epoch: float) -> None:
        """Recording input uses captured UTC, never the wall clock of playback."""
        self._feed(data, first_sample_epoch)

    def _feed(self, data: bytes, first_sample_epoch: float | None) -> None:
        if not data or len(data) % 4:
            return
        if self._window_epoch is None:
            # Live IQ currently has no capture UTC timestamp. Estimate the first
            # sample from receipt time, then use the sample clock continuously.
            first_epoch = (
                first_sample_epoch
                if first_sample_epoch is not None
                else time.time() - len(data) / (4 * self.sample_rate_hz)
            )
            self._window_epoch = math.ceil(first_epoch / _WINDOW_S) * _WINDOW_S
            filter_delay_s = (len(self._taps) - 1) / (2 * self.sample_rate_hz)
            self._skip_audio = math.ceil(
                (self._window_epoch - first_epoch + filter_delay_s) * _AUDIO_RATE
            )
        self._iq_pending.extend(data)
        block_bytes = self._block_samples * 4
        while len(self._iq_pending) >= block_bytes:
            block = bytes(self._iq_pending[:block_bytes])
            del self._iq_pending[:block_bytes]
            self._channelize(block)

    def _channelize(self, data: bytes) -> None:
        raw = np.frombuffer(data, dtype="<i2").astype(np.float32).reshape(-1, 2)
        iq = ((raw[:, 0] + 1j * raw[:, 1]) / 32768).astype(np.complex64)
        idx = np.arange(self._input_count, self._input_count + len(iq), dtype=np.float64)
        iq *= np.exp(
            -2j * np.pi * np.remainder(idx * self.offset_hz / self.sample_rate_hz, 1)
        ).astype(np.complex64)
        joined = np.concatenate((self._tail, iq))
        # FFT convolution makes the rate-scaled USB filter affordable at MHz
        # capture rates, while overlap retains continuity between input blocks.
        filtered = np.fft.ifft(np.fft.fft(joined, self._fft_size) * self._filter_fft)
        filtered = filtered[len(self._taps) - 1 : len(self._taps) - 1 + len(iq)]
        first = (-self._input_count) % self._decimation
        audio = filtered[first :: self._decimation].real
        self._tail = joined[-(len(self._taps) - 1) :].copy()
        self._input_count += len(iq)
        skip = min(self._skip_audio, len(audio))
        self._skip_audio -= skip
        audio = audio[skip:]
        pcm = np.clip(audio * 0.8 * 32767, -32768, 32767).astype("<i2").tobytes()
        self._audio_pending.extend(pcm)
        while len(self._audio_pending) >= self._window_bytes:
            block = bytes(self._audio_pending[: self._window_bytes])
            del self._audio_pending[: self._window_bytes]
            epoch = self._window_epoch
            self._window_epoch += _WINDOW_S
            try:
                self._windows.put_nowait((block, epoch))
            except asyncio.QueueFull:
                self.dropped_windows += 1

    def _make_taps(self) -> np.ndarray:
        # Select 0..3 kHz USB before taking the real component. A short fixed
        # low-pass at MHz input rates aliases other channels and folds LSB in.
        half = 32 * max(1, self._decimation)
        n = np.arange(2 * half + 1, dtype=np.float64) - half
        cutoff = 1_500.0 / max(1, self.sample_rate_hz)
        taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(len(n))
        taps /= taps.sum()
        return (taps * np.exp(2j * np.pi * cutoff * n)).astype(np.complex64)

    def events(self):
        async def _gen():
            while True:
                yield await self._events.get()

        return _gen()

    async def _decode_loop(self) -> None:
        assert self._temp is not None
        root = Path(self._temp.name)
        wav_path = None
        while True:
            item = await self._windows.get()
            if item is None:
                return
            block, epoch = item
            try:
                if wav_path is not None:
                    wav_path.unlink(missing_ok=True)
                filename = dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%y%m%d_%H%M%S")
                wav_path = root / f"{filename}.wav"
                with wave.open(str(wav_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(_AUDIO_RATE)
                    wav.writeframes(block)
                proc = await asyncio.create_subprocess_exec(
                    self.binary,
                    "-8",
                    "-p",
                    "15",
                    "-m",
                    "1",
                    str(wav_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=root,
                )
                try:
                    try:
                        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25)
                    except TimeoutError:
                        self.error = "jt9 exceeded the 25-second FT8 decode limit"
                        continue
                finally:
                    # A cancelled session must not leave a decoder process
                    # running against a WAV file that stop() is about to remove.
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        await proc.wait()
                if proc.returncode:
                    self.error = (
                        stderr.decode("utf-8", errors="replace")[-1000:].strip()
                        or f"jt9 exited {proc.returncode}"
                    )
                    continue
                self.error = None
                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    event = parse_jt9_line(line, window_epoch=epoch)
                    if event is not None:
                        self._events.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc) or type(exc).__name__

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await reap_cancelled(self._worker)
            self._worker = None
        self._windows = asyncio.Queue(maxsize=1)
        self._audio_pending.clear()
        self._iq_pending.clear()
        self._tail.fill(0)
        self._input_count = 0
        self._window_epoch = None
        self._skip_audio = 0
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None
