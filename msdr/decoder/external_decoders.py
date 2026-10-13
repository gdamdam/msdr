"""External-program decoders added beyond multimon/dsd-fme: rtl_433, FreeDV and the
radiosonde_auto_rx demodulators.

* rtl_433 and the sonde demodulators run on the raw-IQ path: msdr shifts the tuned
  signal to baseband, resamples it to the rate the program wants and writes it to
  the child's stdin (`ExternalIQBackend`).
* freedv_rx runs on the USB audio path (`FreeDVBackend`), like the other
  ProcessBackend decoders.

Nothing here was validated against a live signal; see the capability matrix.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

import numpy as np

from msdr.decoder.backends import (
    DecodeEvent,
    DecoderError,
    ProcessBackend,
    policy_integrity,
)
from msdr.decoder.pcm import Resampler, wav_stream_header
from msdr.decoder.registry import DecoderSpec

# IQ is ~1 MB/s at 250 kS/s cs16 (ProcessBackend's own cap is sized for audio):
# allow ~2 s of backlog, then drop rather than stall the shared IQ pump.
_MAX_IQ_QUEUED_BYTES = 4_000_000

# rtl_433 field order for the one-line summary; everything else stays in `fields`.
_RTL433_SUMMARY = (
    ("temperature_C", "{:g} C"),
    ("temperature_F", "{:g} F"),
    ("humidity", "{:g}% RH"),
    ("pressure_hPa", "{:g} hPa"),
    ("pressure_kPa", "{:g} kPa"),
    ("wind_avg_km_h", "wind {:g} km/h"),
    ("rain_mm", "rain {:g} mm"),
    ("battery_ok", "battery_ok={}"),
)


# -- IQ shift + resample ---------------------------------------------------------------------


class IQChannelizer:
    """Streaming mix-to-baseband and resample: int16 IQ bytes -> complex64 at `rate_out`.

    A polyphase windowed-sinc FIR decimates the bulk of a multi-MS/s input (cost is
    taps/D per input sample, not taps), octave FIR stages finish the anti-aliasing, and
    pcm.Resampler (one per I/Q rail, so both stay sample-aligned) lands on the exact rate.
    numpy only: scipy is not a dependency.
    """

    def __init__(self, rate_in: int, offset_hz: float, rate_out: int):
        if rate_in < rate_out:
            raise DecoderError(f"needs IQ rate >= {rate_out} samples/s")
        if abs(offset_hz) > rate_in / 2:
            raise DecoderError("signal falls outside the available IQ span")
        self.rate_in = int(rate_in)
        self.offset_hz = float(offset_hz)
        self.rate_out = int(rate_out)
        self._count = 0
        self._d1 = max(1, self.rate_in // (4 * self.rate_out))
        self._rem = np.empty(0, np.complex64)
        # Cutoff at 0.45 of the post-decimation Nyquist-limited rate, 8 taps per branch.
        self._k = 8
        n = np.arange(self._k * self._d1) - (self._k * self._d1 - 1) / 2
        fc = 0.45 / self._d1
        h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(len(n))
        self._h = (h / h.sum()).astype(np.float32).reshape(self._k, self._d1)
        rate = self.rate_in / self._d1
        n = np.arange(49) - 24
        h = 0.45 * np.sinc(0.45 * n) * np.hamming(49)
        self._taps = (h / h.sum()).astype(np.float32)
        self._stages: list[list] = []
        while rate / 2 >= 1.5 * self.rate_out:
            self._stages.append([np.zeros(48, np.complex64), 0])
            rate /= 2
        self._ri = Resampler(int(round(rate)), self.rate_out)
        self._rq = Resampler(int(round(rate)), self.rate_out)

    def _decimate(self, x: np.ndarray) -> np.ndarray:
        d, k = self._d1, self._k
        buf = np.concatenate((self._rem, x))
        m = (len(buf) - k * d) // d + 1
        if m <= 0:
            self._rem = buf
            return np.empty(0, np.complex64)
        y = np.zeros(m, np.complex64)
        for q in range(k):
            y += buf[q * d : q * d + m * d].reshape(m, d) @ self._h[q]
        self._rem = buf[m * d :]
        return y

    def process(self, data: bytes) -> np.ndarray:
        raw = np.frombuffer(data[: len(data) & ~3], dtype="<i2").astype(np.float32)
        raw = raw.reshape(-1, 2)
        x = ((raw[:, 0] + 1j * raw[:, 1]) / 32768).astype(np.complex64)
        if not len(x):
            return np.empty(0, np.complex64)
        if self.offset_hz:
            idx = np.arange(self._count, self._count + len(x), dtype=np.float64)
            phase = np.remainder(idx * self.offset_hz / self.rate_in, 1)
            x = x * np.exp(-2j * np.pi * phase).astype(np.complex64)
        self._count += len(x)
        if self._d1 > 1:
            x = self._decimate(x)
        for stage in self._stages:
            if not len(x):
                break
            tail, count = stage
            joined = np.concatenate((tail, x))
            filtered = np.convolve(joined, self._taps, mode="valid")
            x = filtered[count & 1 :: 2].astype(np.complex64)
            stage[0] = joined[-48:].copy()
            stage[1] = count + len(filtered)
        if not len(x):
            return np.empty(0, np.complex64)
        return (self._ri.process(x.real) + 1j * self._rq.process(x.imag)).astype(np.complex64)


def _to_cs16(iq: np.ndarray, gain: float) -> bytes:
    out = np.empty((len(iq), 2), dtype="<i2")
    out[:, 0] = np.clip(iq.real * gain * 32768, -32768, 32767)
    out[:, 1] = np.clip(iq.imag * gain * 32768, -32768, 32767)
    return out.tobytes()


class ExternalIQBackend(ProcessBackend):
    """Raw-IQ ProcessBackend: IQSession calls feed() with int16 interleaved device IQ."""

    def __init__(
        self,
        argv,
        parser,
        *,
        decoder_id: str,
        sample_rate_hz: int,
        center_hz: float,
        frequency_hz: float,
        out_rate: int,
        preamble: bytes = b"",
        agc: bool = False,
    ):
        super().__init__(argv, parser, preamble=preamble, decoder_id=decoder_id)
        self.backend_name = Path(argv[0]).name
        self._chan = IQChannelizer(sample_rate_hz, frequency_hz - center_hz, out_rate)
        # FM demodulators (sondes) are amplitude-blind but 16-bit quantisation of a weak
        # channel is not, so they get a slow AGC. rtl_433 measures levels itself and gets
        # unity gain so its -M level readings stay meaningful.
        self._agc = agc
        self._power = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await super().start()

    def _deliver(self, data: bytes) -> None:
        if self._queued_bytes + len(data) > _MAX_IQ_QUEUED_BYTES:
            self.dropped_frames += 1
            return
        self._write_buf.append(data)
        self._queued_bytes += len(data)
        self._write_ready.set()

    def feed(self, data: bytes) -> None:
        if self._proc is None or not data:
            return
        iq = self._chan.process(data)
        if not len(iq):
            return
        gain = 1.0
        if self._agc:
            self._power = (
                0.9 * self._power + 0.1 * float(np.mean(np.abs(iq) ** 2))
                if self._power
                else float(np.mean(np.abs(iq) ** 2))
            )
            gain = min(1000.0, 0.25 / math.sqrt(self._power)) if self._power > 0 else 1.0
        payload = _to_cs16(iq, gain)
        # IQDecoderSession feeds from a worker thread; asyncio primitives are loop-only.
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop or self._loop is None:
            self._deliver(payload)
        else:
            self._loop.call_soon_threadsafe(self._deliver, payload)


# -- rtl_433 ---------------------------------------------------------------------------------


def parse_rtl433_line(line: str, decoder_id: str) -> DecodeEvent | None:
    """One `-F json` object per line. Needs a `model`; every other key passes through."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "model" not in obj:
        return None
    fields = {k: v for k, v in obj.items() if k != "time"}
    if "time" in obj:
        fields["sensor_time"] = obj["time"]
    parts = [str(obj["model"])]
    if "id" in obj:
        parts.append(str(obj["id"]))
    summary = [fmt.format(obj[key]) for key, fmt in _RTL433_SUMMARY if key in obj]
    text = " ".join(parts) + (": " + ", ".join(summary) if summary else "")
    return DecodeEvent(
        ts=time.time(),
        decoder=decoder_id,
        kind="text",
        text=text,
        fields=fields,
        **policy_integrity(decoder_id, "rtl_433"),
    )


def _rtl433_backend(spec, binaries, *, sample_rate_hz, center_hz, frequency_hz):
    path = _require(spec, binaries, "rtl_433")
    argv = [
        path,
        "-r", "cs16:-",
        "-s", str(spec.input_rate),
        "-f", str(int(frequency_hz)),
        "-F", "json",
        "-M", "time:unix",
        "-M", "level",
        "-M", "protocol",
        *spec.args,
    ]  # fmt: skip
    return ExternalIQBackend(
        argv,
        lambda line: parse_rtl433_line(line, spec.id),
        decoder_id=spec.id,
        sample_rate_hz=sample_rate_hz,
        center_hz=center_hz,
        frequency_hz=frequency_hz,
        out_rate=spec.input_rate,
    )


# -- radiosonde_auto_rx demodulators ---------------------------------------------------------

_SONDE_IQ_RATE = 48_000


def _num(obj: dict, key: str):
    value = obj.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_sonde_json(line: str, decoder_id: str) -> DecodeEvent | None:
    """dfm09mod/m10mod/m20mod/rs92mod `--json` line -> the field names the Map reads for
    RS41 (serial, lat, lon, altitude_m, heading_deg) plus the demodulator's telemetry."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    serial = obj.get("id") if isinstance(obj, dict) else None
    if not isinstance(serial, str) or not serial:
        return None
    fields: dict[str, object] = {"serial": serial}
    for src, dst in (
        ("type", "sonde_type"),
        ("subtype", "sonde_subtype"),
        ("frame", "frame_number"),
        ("datetime", "sonde_time"),
        ("sats", "gps_satellites"),
        ("batt", "battery_v"),
        ("temp", "temperature_c"),
        ("humidity", "humidity_pct"),
        ("pressure", "pressure_hpa"),
        ("vel_h", "vel_h_m_s"),
        ("vel_v", "vel_v_m_s"),
        ("heading", "heading_deg"),
    ):
        if obj.get(src) is not None:
            fields[dst] = obj[src]
    lat, lon, alt = _num(obj, "lat"), _num(obj, "lon"), _num(obj, "alt")
    # 0,0 is the demodulators' "no GPS fix yet" (and rs92mod without ephemeris); mapping it
    # would put every sonde in the Gulf of Guinea.
    if lat is not None and lon is not None and (lat or lon) and abs(lat) <= 90 and abs(lon) <= 180:
        fields["lat"], fields["lon"] = lat, lon
        if alt is not None:
            fields["altitude_m"] = alt
    kind = fields.get("sonde_type", "sonde")
    text = f"{kind} {serial}" + "".join(
        f" {label}={fields[key]}"
        for key, label in (("frame_number", "frame"), ("altitude_m", "alt_m"))
        if key in fields
    )
    return DecodeEvent(
        ts=time.time(),
        decoder=decoder_id,
        kind="text",
        text=text,
        fields=fields,
        **policy_integrity(decoder_id, "radiosonde_auto_rx"),
    )


def _sonde_backend(spec, binaries, *, sample_rate_hz, center_hz, frequency_hz):
    path = _require(spec, binaries, spec.binaries[0])
    # --iq0: the demodulator does the FM discrimination itself from a stereo IQ WAV,
    # so msdr only has to deliver baseband IQ (no deviation scaling on our side).
    argv = [path, *spec.args, "-"]
    return ExternalIQBackend(
        argv,
        lambda line: parse_sonde_json(line, spec.id),
        decoder_id=spec.id,
        sample_rate_hz=sample_rate_hz,
        center_hz=center_hz,
        frequency_hz=frequency_hz,
        out_rate=_SONDE_IQ_RATE,
        preamble=wav_stream_header(_SONDE_IQ_RATE, channels=2),
        agc=True,
    )


def _require(spec: DecoderSpec, binaries: dict[str, str | None], name: str) -> str:
    path = binaries.get(name)
    if not path:
        raise DecoderError(f"{spec.id}: required binary '{name}' not found")
    return path


def build_external_iq_backend(spec, binaries, *, sample_rate_hz, center_hz, frequency_hz):
    """Entry point used by manager.py and iq_backend.py for spec.backend == "external_iq"."""
    kw = dict(sample_rate_hz=sample_rate_hz, center_hz=center_hz, frequency_hz=frequency_hz)
    if spec.binaries == ("rtl_433",):
        return _rtl433_backend(spec, binaries, **kw)
    return _sonde_backend(spec, binaries, **kw)


# -- FreeDV ----------------------------------------------------------------------------------

# freedv_rx -v prints one line per modem frame to stderr (codec2 src/freedv_rx.c):
#   frame: 12  sync: 1  nin: 960 snr: 4.50 dB  bit errors: 0 clock_off: ... foff: ...
_FREEDV_STATS = re.compile(r"^frame:\s*(\d+)\s+sync:\s*(\d)\s+nin:\s*\d+\s+snr:\s*(-?[\d.]+)\s*dB")
# Other stderr lines (usage, final BER summary) must not become "received text".
_FREEDV_CHATTER = re.compile(r"^(frames decoded|BER|Coded|usage|Error|example)", re.I)
_SNR_REPORT_S = 10.0


class FreeDVState:
    def __init__(self, decoder_id: str, mode: str):
        self.decoder_id = decoder_id
        self.mode = mode
        self.synced: bool | None = None
        self._last_report = 0.0

    def _event(self, text, fields, integrity=None) -> DecodeEvent:
        return DecodeEvent(
            ts=time.time(),
            decoder=self.decoder_id,
            kind="text",
            text=text,
            fields={"mode": self.mode, **fields},
            **(integrity or {"integrity": "not_applicable", "integrity_note": "sync status"}),
        )

    def parse_stderr(self, line: str) -> DecodeEvent | None:
        match = _FREEDV_STATS.match(line.strip())
        if not match:
            return None
        frame, sync, snr = int(match[1]), match[2] == "1", float(match[3])
        now = time.monotonic()
        fields = {"sync": sync, "snr_db": snr, "frame": frame}
        if sync != self.synced:
            first, self.synced, self._last_report = self.synced is None, sync, now
            if first and not sync:
                return None  # initial "not synced" is the idle state, not news
            state = "sync acquired" if sync else "sync lost"
            return self._event(f"FreeDV {self.mode} {state}, SNR {snr:.1f} dB", fields)
        if sync and now - self._last_report >= _SNR_REPORT_S:
            self._last_report = now
            return self._event(f"FreeDV {self.mode} synced, SNR {snr:.1f} dB", fields)
        return None

    def parse_text(self, line: str) -> DecodeEvent | None:
        text = "".join(ch for ch in line if ch.isprintable()).strip()
        if not text or _FREEDV_CHATTER.match(text):
            return None
        return self._event(
            text,
            {"received_text": text, "sync": True},
            policy_integrity(self.decoder_id, "freedv_rx"),
        )


class FreeDVBackend(ProcessBackend):
    """freedv_rx on 8 kHz s16 audio. Text arrives through a side file (--txtrx), sync and
    SNR through stderr; the decoded speech is discarded (/dev/null), so msdr shows
    text and sync only."""

    def __init__(self, argv, state: FreeDVState, text_path: Path):
        super().__init__(
            argv,
            state.parse_stderr,
            decoder_id=state.decoder_id,
            merge_stderr=True,
            event_log_path=text_path,
        )
        self._text_parser = state.parse_text
        self._text_path = text_path
        self.freedv_state = state

    def _read_event_log_available(self) -> None:
        # Same polling loop as dsd-fme's -J log, but these lines are text, not stats.
        saved, self._parser = self._parser, self._text_parser
        try:
            super()._read_event_log_available()
        finally:
            self._parser = saved

    async def stop(self) -> None:
        # freedv_rx's --txtrx FILE is stdio-buffered and only flushed at exit, so let it
        # see EOF and drain the file before the normal teardown cancels the poller.
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            self._read_event_log_available()
        try:
            await super().stop()
        finally:
            with contextlib.suppress(OSError):
                self._text_path.unlink()


def build_freedv_backend(spec: DecoderSpec, binaries: dict[str, str | None]) -> FreeDVBackend:
    path = _require(spec, binaries, "freedv_rx")
    mode = spec.args[0]
    fd, name = tempfile.mkstemp(prefix=f"{spec.id}-", suffix="-text.log")
    os.close(fd)
    argv = [path, "-v"]
    # Reliable text (callsign) exists on 700D/700E only; 1600 uses the legacy char callback.
    if mode in ("700D", "700E"):
        argv += ["--reliabletext", "MSDR"]
    argv += ["--txtrx", name, mode, "-", os.devnull]
    return FreeDVBackend(argv, FreeDVState(spec.id, mode), Path(name))
