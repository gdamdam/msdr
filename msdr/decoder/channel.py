"""One audio source -> resample -> normalise -> backend pipeline per decoder."""

from collections.abc import AsyncIterator
from typing import Literal

from msdr.decoder.backends import DecodeEvent, ProcessBackend
from msdr.decoder.pcm import Normalizer, Resampler, to_mono_f32, to_s16
from msdr.decoder.registry import DecoderSpec


class DecodeChannel:
    def __init__(self, spec: DecoderSpec, backend: ProcessBackend, in_rate: int, in_channels: int):
        self.spec = spec
        self._backend = backend
        self.in_channels = in_channels
        self._resampler = (
            Resampler(in_rate, spec.input_rate) if in_rate != spec.input_rate else None
        )
        self._normalizer = Normalizer(rate=spec.input_rate) if spec.normalize else None
        self.state: Literal["starting", "running", "error", "stopped"] = "starting"
        self.error: str | None = None

    def feed(self, frame_bytes: bytes) -> None:
        mono = to_mono_f32(frame_bytes, self.in_channels)
        x = self._resampler.process(mono) if self._resampler is not None else mono
        if self._normalizer is not None:
            x = self._normalizer.process(x)
        self._backend.feed(to_s16(x))

        # Opportunistic idle-flush for char-streamed backends (minimodem):
        # feed() runs at audio-frame cadence (~20 Hz), which stands in for
        # "the channel's timer" without a dedicated per-channel task.
        coalescer = getattr(self._backend, "idle_coalescer", None)
        if coalescer is not None:
            event = coalescer.flush()
            if event is not None:
                self._backend.push_event(event)

    def reconfigure(self, in_rate: int) -> None:
        """Rebuild the resampler when the observed audio rate differs from configured."""
        self._resampler = (
            Resampler(in_rate, self.spec.input_rate) if in_rate != self.spec.input_rate else None
        )

    def events(self) -> AsyncIterator[DecodeEvent]:
        return self._backend.events()

    @property
    def backend_error(self) -> str | None:
        return self._backend.error

    async def stop(self) -> None:
        await self._backend.stop()
