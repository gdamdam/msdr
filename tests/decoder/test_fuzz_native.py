"""Fuzz the in-process decoders with hostile input: noise, zeros, empty,
very short, NaN/inf, huge values, random bytes. Contract: no exception and
bounded runtime -- a decoder may emit nothing, or set `.error`, but must not
crash the channel or hang. Fixed seed; the point is robustness, not decoding."""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.decoder.pydecoders.adsb import NativeADSBBackend
from msdr.decoder.pydecoders.ais import AisDecoder
from msdr.decoder.pydecoders.dsc import DSCBackend
from msdr.decoder.pydecoders.endoftrain import EndOfTrainBackend
from msdr.decoder.pydecoders.lora import LoRaBackend
from msdr.decoder.pydecoders.navigation import NavigationIQDecoder
from msdr.decoder.pydecoders.navtex import NavtexDecoder
from msdr.decoder.pydecoders.rs41 import RS41Backend
from msdr.decoder.pydecoders.rtty import RttyDecoder
from msdr.decoder.pydecoders.sstv import SstvDecoder
from msdr.decoder.pydecoders.timecode import Dcf77Decoder, MsfDecoder, WwvDecoder
from msdr.decoder.pydecoders.wefax import WefaxDecoder

_SEED = 20261013
_MAX_S = 8.0  # per decoder x input; real cost is well below this
_AUDIO_S = 0.5

_AUDIO = {
    "ais": lambda d: AisDecoder(),
    "navtex": lambda d: NavtexDecoder(),
    "rtty": lambda d: RttyDecoder(),
    "sstv": lambda d: SstvDecoder(d),
    "wefax": lambda d: WefaxDecoder(d),
    "wwv": lambda d: WwvDecoder(),
    "dcf77": lambda d: Dcf77Decoder(),
    "msf": lambda d: MsfDecoder(),
}


def _mk(ctor, rate, *, spec=None, freq=100e6, **kw):
    return lambda: ctor(spec, sample_rate_hz=rate, center_hz=100e6, frequency_hz=freq, **kw)


_IQ = {
    "adsb_2.4M": (_mk(NativeADSBBackend, 2_400_000), 2_400_000),
    "adsb_2M": (_mk(NativeADSBBackend, 2_000_000), 2_000_000),
    "rs41": (_mk(RS41Backend, 57_600), 57_600),
    "lora": (_mk(LoRaBackend, 250_000), 250_000),
    "dsc_hf": (_mk(DSCBackend, 48_000), 48_000),
    "dsc_vhf": (_mk(DSCBackend, 48_000, freq=156_525_000), 48_000),
    "endoftrain": (_mk(EndOfTrainBackend, 48_000), 48_000),
    "vor": (_mk(NavigationIQDecoder, 48_000, spec=SimpleNamespace(id="vor")), 48_000),
    "ils": (_mk(NavigationIQDecoder, 48_000, spec=SimpleNamespace(id="ils")), 48_000),
}

_KINDS = ["noise", "zeros", "empty", "short", "nan", "inf", "huge", "constant_fullscale"]


def _float_inputs(kind: str, n: int, rng: np.random.Generator) -> list[np.ndarray]:
    noise = rng.standard_normal(n).astype(np.float32)
    if kind == "noise":
        return [noise]
    if kind == "zeros":
        return [np.zeros(n, np.float32)]
    if kind == "empty":
        return [np.zeros(0, np.float32)]
    if kind == "short":
        return [noise[:1], noise[:3], noise[:17], noise[:101]]
    if kind == "nan":
        bad = noise.copy()
        bad[rng.choice(n, n // 50, replace=False)] = np.nan
        return [bad, np.full(n, np.nan, np.float32)]
    if kind == "inf":
        bad = noise.copy()
        bad[rng.choice(n, n // 50, replace=False)] = rng.choice([np.inf, -np.inf], n // 50)
        return [bad, np.full(n, np.inf, np.float32)]
    if kind == "huge":
        return [noise * np.float32(3e38), noise * np.float32(1e30)]
    return [np.full(n, 1.0, np.float32)]  # constant_fullscale: a stuck carrier


def _byte_inputs(kind: str, n: int, rng: np.random.Generator) -> list[bytes]:
    """Interleaved int16 IQ as raw bytes; n = sample count."""
    with np.errstate(all="ignore"):
        floats = _float_inputs(kind, 2 * n, rng)
        if kind in ("nan", "inf", "huge"):
            # int16 cannot carry these; casting is what a broken upstream
            # float->int stage would hand us (and random bytes cover the rest).
            return [f.astype("<i2").tobytes() for f in floats] + [
                rng.integers(0, 256, 4 * n, dtype=np.uint8).tobytes(),
                rng.integers(0, 256, 4 * n + 3, dtype=np.uint8).tobytes(),  # odd length
            ]
        if kind == "constant_fullscale":
            return [np.full(2 * n, 32767, "<i2").tobytes(), np.full(2 * n, -32768, "<i2").tobytes()]
        if kind == "noise":
            return [
                (f * 8000).astype("<i2").tobytes() for f in floats
            ] + [rng.integers(0, 256, 4 * n, dtype=np.uint8).tobytes()]
        if kind == "short":
            return [bytes([1]), bytes([1, 2, 3]), bytes(4), bytes(6), bytes(range(40))]
        return [f.astype("<i2").tobytes() for f in floats]


def _bounded(start: float, what: str) -> None:
    assert time.monotonic() - start < _MAX_S, f"{what}: took over {_MAX_S}s (hang?)"


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("name", list(_AUDIO))
def test_audio_decoder_survives_hostile_input(name, kind, tmp_path):
    rng = np.random.default_rng(_SEED)
    decoder = _AUDIO[name](tmp_path)
    n = int(decoder.input_rate * _AUDIO_S)
    t0 = time.monotonic()
    for chunk in _float_inputs(kind, n, rng):
        events = decoder.feed(chunk)
        assert isinstance(events, list)
    assert isinstance(decoder.flush(), list)
    # Decoder must still work on a following ordinary block (no poisoned state crash).
    decoder.feed(rng.standard_normal(n).astype(np.float32))
    decoder.flush()
    _bounded(t0, f"{name}/{kind}")


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("name", list(_IQ))
async def test_iq_decoder_survives_hostile_input(name, kind):
    rng = np.random.default_rng(_SEED)
    factory, rate = _IQ[name]
    backend = factory()
    await backend.start()
    t0 = time.monotonic()
    try:
        for data in _byte_inputs(kind, int(rate * 0.1), rng):
            backend.feed(data)
        # Still alive after garbage: an ordinary block must not raise either.
        backend.feed(_byte_inputs("noise", int(rate * 0.05), rng)[0])
    finally:
        await backend.stop()
    _bounded(t0, f"{name}/{kind}")
