"""Signal decoders: multimon-ng / minimodem backends fed from SDRconnect's
demodulated audio stream. Optional at import like msdr/recorder, so a host
without the binaries (or without numpy) still runs msdr with decoding off."""

from msdr.decoder.audio_stream import AudioFrame, AudioStream
from msdr.decoder.backends import DecodeEvent, DecoderError
from msdr.decoder.manager import DecoderManager
from msdr.decoder.registry import DECODERS, available, describe, resolve_binaries

__all__ = [
    "DecoderManager",
    "AudioStream",
    "AudioFrame",
    "DecoderError",
    "DecodeEvent",
    "DECODERS",
    "describe",
    "available",
    "resolve_binaries",
]
