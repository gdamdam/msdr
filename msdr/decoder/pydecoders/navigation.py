"""Small in-process VOR and ILS measurement decoders for raw complex IQ.

These return unvalidated measurements only, with no integrity check on the
radial/DDM themselves. Station identity (the ~1020 Hz Morse ident) is
decoded by ``msdr.decoder.pydecoders.morse_ident`` -- see that module for
the SDRangel port it's based on -- and is only exposed once the same text
has been heard twice in a row; until then (or if reception is too poor to
decode any ident) the ``ident*`` fields are simply absent. Either way, the
output is not suitable for navigation without independent validation.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np

from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.pydecoders.morse_ident import MorseIdentDecoder
from msdr.decoder.pydecoders.rs41 import _StreamingResampler


def _tone(x: np.ndarray, rate: float, hz: float) -> complex:
    n = len(x)
    if n == 0:
        return 0j
    phase = 2 * np.pi * hz * np.arange(n) / rate
    return 2 * np.mean(x * np.exp(-1j * phase))


class NavigationIQDecoder:
    """Raw-IQ decoder with the backend surface used by ``IQDecoderSession``.

    ``spec.id`` must be ``vor`` or ``ils``. IQ is signed little-endian
    interleaved int16. A measurement is emitted after at least one second.
    """

    def __init__(self, spec, *, sample_rate_hz: int, center_hz: float, frequency_hz: float):
        self.spec = spec
        self.decoder_id = spec.id
        if self.decoder_id not in {"vor", "ils"}:
            raise DecoderError("navigation decoder supports only VOR and ILS")
        self.input_rate = int(sample_rate_hz)
        self.rate = self.input_rate
        self.center_hz = float(center_hz)
        self.frequency_hz = float(frequency_hz)
        if self.input_rate < 24_000:
            raise DecoderError("VOR/ILS requires at least 24 kHz IQ sample rate")
        bandwidth = 11_000 if self.decoder_id == "vor" else 3_000
        if abs(self.frequency_hz - self.center_hz) + bandwidth > self.input_rate / 2:
            raise DecoderError("navigation channel falls outside the available IQ span")
        self._resampler = (
            _StreamingResampler(self.input_rate, self.frequency_hz - self.center_hz)
            if self.input_rate >= 57_600
            else None
        )
        if self._resampler is not None:
            self.rate = 57_600
        self.error: str | None = None
        self._iq = np.empty(0, dtype=np.complex128)
        # Persistent across blocks: idents repeat every ~10-30 s, and the
        # two-repeat confirmation needs to remember the previous decode.
        self._morse = MorseIdentDecoder(self.rate)
        self._events: asyncio.Queue[DecodeEvent | None] = asyncio.Queue()
        self._running = False

    async def start(self) -> None:
        self._running = True

    def feed(self, data: bytes) -> None:
        if not self._running or not data:
            return
        if len(data) % 4:
            self.error = "incomplete interleaved int16 IQ pair"
            return
        if self._resampler is not None:
            z = self._resampler.process(data)
        else:
            raw = np.frombuffer(data, dtype="<i2").astype(np.float64).reshape(-1, 2)
            z = (raw[:, 0] + 1j * raw[:, 1]) / 32768.0
        # Keep bounded measurement windows. Analyze each complete second.
        self._iq = np.concatenate((self._iq, z))
        window = self.rate
        while len(self._iq) >= window:
            block, self._iq = self._iq[:window], self._iq[window:]
            event = self._measure(block)
            if event is not None:
                self._events.put_nowait(event)

    def _measure(self, iq: np.ndarray) -> DecodeEvent | None:
        offset = 0.0 if self._resampler is not None else self.frequency_hz - self.center_hz
        t = np.arange(len(iq)) / self.rate
        tuned = iq * np.exp(-2j * np.pi * offset * t)
        # A frequency shift alone does not isolate the selected station:
        # abs(tuned) equals abs(iq). Limit the baseband before AM detection.
        cutoff = 10_500 if self.decoder_id == "vor" else 2_500
        transition = 500.0
        frequencies = np.abs(np.fft.fftfreq(len(tuned), 1 / self.rate))
        response = np.clip((cutoff + transition - frequencies) / transition, 0.0, 1.0)
        magnitude = np.abs(np.fft.ifft(np.fft.fft(tuned) * response))
        # Feed the ident decoder every block, even ones too weak to yield a
        # radial/DDM below -- its timing state needs unbroken sample
        # coverage, and a confirmed ident is simply picked up by whichever
        # later block does produce a measurement event.
        self._morse.process(magnitude)
        carrier = float(np.mean(magnitude))
        if not np.isfinite(carrier) or carrier < 0.002:
            return None

        if self.decoder_id == "ils":
            envelope = magnitude - np.mean(magnitude)
            d90 = abs(_tone(envelope, self.rate, 90)) / carrier
            d150 = abs(_tone(envelope, self.rate, 150)) / carrier
            if max(d90, d150) < 0.005:
                return None
            ddm = d90 - d150
            fields = {
                "ddm": float(ddm),
                "depth_90_hz": float(d90),
                "depth_150_hz": float(d150),
                "measurement_status": "unvalidated",
            }
            text = f"ILS DDM {ddm:+.4f} (unvalidated measurement)"
        else:
            # FM discriminator recovers the 30 Hz reference plus the 9960 Hz
            # subcarrier, whose frequency deviation carries the variable phase.
            envelope = magnitude - np.mean(magnitude)
            ref = _tone(envelope, self.rate, 30)
            if abs(ref) / carrier < 0.005:
                return None
            n = len(envelope)
            tt = np.arange(n) / self.rate
            mixed = envelope * np.exp(-2j * np.pi * 9960 * tt)
            # A short boxcar suppresses the opposite-frequency image while
            # retaining the subcarrier's 30 Hz FM sidebands.
            k = max(1, int(self.rate / 2000))
            smooth = np.convolve(mixed, np.ones(k) / k, mode="same")
            subphase = np.unwrap(np.angle(smooth))
            deviation = np.diff(subphase) * self.rate / (2 * np.pi)
            var = _tone(deviation, self.rate, 30)
            if abs(var) < 10:
                return None
            # The reference phase is the zero-degree datum; FM frequency
            # deviation has the opposite phase sign to its modulation phase.
            radial = float((np.degrees(np.angle(ref) - np.angle(var))) % 360)
            fields = {
                "radial_deg": radial,
                "reference_30_hz": float(abs(ref)),
                "variable_30_hz": float(abs(var)),
                "measurement_status": "unvalidated",
            }
            text = f"VOR radial {radial:.1f}° (unvalidated measurement)"
        if self._morse.ident is not None:
            fields["ident"] = self._morse.ident
            fields["ident_repeats"] = self._morse.ident_repeats
            fields["ident_confidence"] = self._morse.ident_confidence
            text += f", ident {self._morse.ident}"
        return DecodeEvent(
            ts=time.time(),
            decoder=self.decoder_id,
            kind="measurement",
            text=text,
            fields=fields,
            integrity="unavailable",
            integrity_note=(
                "Unvalidated navigation measurement; radial/DDM has no integrity check. "
                "ident is the Morse station identifier, confirmed only after two "
                "identical decodes; absent fields mean no ident has been confirmed yet."
            ),
        )

    async def get_event(self) -> DecodeEvent | None:
        """Return the next measurement, or ``None`` after stop."""
        return await self._events.get()

    async def events(self):
        while True:
            event = await self.get_event()
            if event is None:
                return
            yield event

    async def stop(self) -> None:
        self._running = False
        self._iq = np.empty(0, dtype=np.complex128)
        self._events.put_nowait(None)
