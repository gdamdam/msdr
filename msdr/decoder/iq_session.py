"""Single-frequency external IQ decoders, isolated from the NFM channelizer."""

import asyncio
import logging
import time
from dataclasses import replace

from msdr.decoder.backends import DecoderError, reap_cancelled
from msdr.decoder.readiness import decoder_facts

logger = logging.getLogger(__name__)


class IQDecoderSession:
    def __init__(self, *, stream, backends, frequency_hz, signal_id, on_event, on_status):
        self._stream = stream
        self._backends = backends
        self._frequency = frequency_hz
        self._signal_id = signal_id
        self._on_event = on_event
        self._on_status = on_status
        self._running = False
        self._tasks = []
        self._last_frame = None
        self._error = None
        self._counts = {key: 0 for key in backends}
        self._valid = {key: 0 for key in backends}
        self._last_event = {}
        self._backend_errors = {}

    async def start(self):
        try:
            for backend in self._backends.values():
                await backend.start()
            await self._stream.start()
            await self._stream.enable(True)
            self._running = True
            self._tasks = [asyncio.create_task(self._pump())]
            self._tasks.extend(
                asyncio.create_task(self._drain(key, backend))
                for key, backend in self._backends.items()
            )
        except BaseException as exc:
            try:
                await self.stop()
            except Exception:
                logger.exception("IQ decoder startup cleanup failed")
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DecoderError(f"IQ decoder startup failed: {exc}") from exc

    async def _pump(self):
        try:
            async for frame in self._stream.frames():
                if not self._running:
                    break
                self._last_frame = time.monotonic()
                for key, backend in self._backends.items():
                    backend.feed(frame.data)
                    if backend.error != self._backend_errors.get(key):
                        self._backend_errors[key] = backend.error
                        self._on_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = str(exc)
            self._on_status()

    async def _drain(self, key, backend):
        try:
            async for event in backend.events():
                if not self._running:
                    break
                self._counts[key] += 1
                self._valid[key] += event.integrity == "passed"
                self._last_event[key] = event.ts
                self._on_event(
                    replace(
                        event,
                        frequency_hz=self._frequency,
                        signal_id=self._signal_id,
                        source_id="live:sdrconnect",
                        fields={
                            **event.fields,
                            "source": "iq",
                            "backend": getattr(self._backends[key], "backend_name", "standalone"),
                        },
                    )
                )
                self._on_status()
            if backend.error:
                self._on_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = str(exc)
            self._on_status()

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            await reap_cancelled(task)
        self._tasks = []
        failure = None
        try:
            await self._stream.stop()
        except BaseException as exc:
            failure = exc
        for backend in self._backends.values():
            try:
                await backend.stop()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        self._last_frame = None
        if failure is not None:
            raise failure

    def status(self):
        facts = []
        stream_dropped = getattr(self._stream, "dropped_count", 0)
        for key, backend in self._backends.items():
            error = self._error or backend.error
            fact = decoder_facts(
                key,
                state="error" if error else "running",
                error=error,
                events=self._counts[key],
                valid_frames=self._valid[key],
                last_event_ts=self._last_event.get(key),
            )
            if hasattr(backend, "dropped_frames"):
                fact["dropped_frames"] = backend.dropped_frames + stream_dropped
            facts.append(fact)
        return {
            "channels": [
                {
                    "decoders": list(self._backends),
                    "decoder_status": facts,
                    "state": "error" if self._error else "running",
                    "error": self._error,
                }
            ],
            "iq": {
                "dropped": stream_dropped,
                "dropped_samples": getattr(self._stream, "dropped_samples", 0),
                "connected": self._last_frame is not None
                and time.monotonic() - self._last_frame < 2
            },
        }
