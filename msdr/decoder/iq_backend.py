"""Raw-IQ backend construction shared by recording analysis consumers."""

import importlib

from msdr.decoder.backends import DecoderError


def build_iq_backend(
    spec, binaries, *, sample_rate_hz, center_hz, frequency_hz, decoder_settings=None
):
    if frequency_hz is None:
        raise DecoderError("Tune the virtual receiver before starting IQ decoding")
    if sample_rate_hz < spec.input_rate:
        raise DecoderError(f"{spec.label}: needs IQ rate >= {spec.input_rate} samples/s")
    if abs(frequency_hz - center_hz) + spec.default_bandwidth_hz / 2 > sample_rate_hz / 2:
        raise DecoderError(f"{spec.label}: full signal bandwidth must fit in recorded IQ span")
    kw = dict(sample_rate_hz=sample_rate_hz, center_hz=center_hz, frequency_hz=frequency_hz)
    if spec.backend == "standalone_adsb":
        from msdr.decoder.standalone_adsb import StandaloneADSBBackend

        return StandaloneADSBBackend(binaries.get("dump1090"), **kw)
    if spec.backend == "standalone_ft8":
        from msdr.decoder.standalone_ft8 import StandaloneFT8Backend

        return StandaloneFT8Backend(binaries.get("jt9"), **kw)
    if spec.backend == "standalone_dsc":
        from msdr.decoder.pydecoders.dsc import DSCBackend

        return DSCBackend(spec, **kw)
    if spec.backend == "standalone_navigation":
        from msdr.decoder.pydecoders.navigation import NavigationIQDecoder

        return NavigationIQDecoder(spec, **kw)
    if spec.backend == "standalone_protocol" and spec.args:
        module, cls = spec.args[0].split(":", 1)
        extra = {}
        if spec.id == "lora" and decoder_settings is not None:
            extra = {
                "spreading_factor": decoder_settings.lora_spreading_factor,
                "bandwidth_hz": decoder_settings.lora_bandwidth_hz,
                "coding_rate": decoder_settings.lora_coding_rate,
                "protocol": decoder_settings.lora_protocol,
            }
        return getattr(importlib.import_module(module), cls)(spec, **kw, **extra)
    raise DecoderError(f"{spec.id}: unsupported raw-IQ backend {spec.backend}")
