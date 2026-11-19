"""PyDecoder: flat NFM discriminator audio (float32, 48 kHz) -> which digital
voice system is on the channel, from its frame sync (msdr.survey.framesync).

It identifies the protocol and nothing else: no call, talkgroup, identity or
audio is decoded, so its events carry integrity "unknown" and never count as
checked frames. Use it to choose the real decoder (dsd-fme) for the channel.
"""

from __future__ import annotations

import time

import numpy as np

from msdr.decoder.backends import DecodeEvent
from msdr.survey.framesync import FrameSync, detect_frame_sync_disc, quiet_only

_FS = 48_000
# The sparse syncs (DMR voice 360 ms, D-STAR 420 ms) need two repeats in view.
_WINDOW = int(1.5 * _FS)
_HOP = _FS
_MIN_FLUSH = _FS  # a shorter tail cannot hold two sparse syncs
# One message per system while it stays on the channel, not one per window.
_REPEAT_S = 30.0


def _text(found: FrameSync) -> str:
    polarity = ", inverted polarity" if found.inverted else ""
    return (
        f"{found.protocol} frame sync seen ({found.sync}, {found.hits} repeats, "
        f"{found.symbol_rate_hz} Bd{polarity}); protocol only, nothing decoded"
    )


class VoiceSyncDecoder:
    input_rate = _FS

    def __init__(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_event: dict[str, float] = {}
        # Read by the backend for decoder status: the latest window held a sync.
        self.synchronized = False

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size:
            self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=np.float32)])
        events: list[DecodeEvent] = []
        while self._buf.size >= _WINDOW:
            events.extend(self._analyse(self._buf[:_WINDOW]))
            self._buf = self._buf[_HOP:]
        return events

    def flush(self) -> list[DecodeEvent]:
        tail, self._buf = self._buf, self._buf[:0]
        return self._analyse(tail) if tail.size >= _MIN_FLUSH else []

    def _analyse(self, block: np.ndarray) -> list[DecodeEvent]:
        found = detect_frame_sync_disc(quiet_only(block, _FS), _FS)
        self.synchronized = bool(found)
        if not found:
            return []
        top = found[0]
        now = time.time()
        if now - self._last_event.get(top.decoder_id, float("-inf")) < _REPEAT_S:
            return []
        self._last_event[top.decoder_id] = now
        return [
            DecodeEvent(
                ts=now,
                decoder="voicesync",
                kind="text",
                text=_text(top),
                fields={**top.to_dict(), "synchronized": True},
                integrity="unknown",
                integrity_note="frame sync pattern only; no frame was decoded or checked",
            )
        ]
