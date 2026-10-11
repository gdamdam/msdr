"""Receiver preparation plans; no tuning or decoding side effects."""

from msdr.decoder.registry import DECODERS

PROFILES = {
    "meshcore_us": {
        "decoder": "lora",
        "label": "MeshCore US (910.525 MHz)",
        "frequency_hz": 910_525_000,
        "decoder_settings": {
            "lora_spreading_factor": 7,
            "lora_bandwidth_hz": 62_500,
            "lora_coding_rate": 1,
            "lora_protocol": "meshcore",
        },
    },
    "meshtastic_us_longfast": {
        "decoder": "lora",
        "label": "Meshtastic US LongFast (906.875 MHz)",
        "frequency_hz": 906_875_000,
        "decoder_settings": {
            "lora_spreading_factor": 11,
            "lora_bandwidth_hz": 250_000,
            "lora_coding_rate": 1,
            "lora_protocol": "meshtastic",
        },
    },
}


def receiver_preset(decoder_id, frequency_hz, props, lna_state=None, profile=None,
                    decoder_settings=None):
    spec = DECODERS.get(decoder_id)
    if spec is None:
        raise ValueError("Unknown decoder")
    profile_spec = PROFILES.get(profile) if profile else None
    if profile and profile_spec is None:
        raise ValueError("Unknown receiver profile")
    if profile_spec and profile_spec["decoder"] != decoder_id:
        raise ValueError("Receiver profile does not match decoder")
    if profile_spec:
        frequency_hz = profile_spec["frequency_hz"]
    hz = frequency_hz if frequency_hz is not None else (
        1_090_000_000 if decoder_id == "adsb" else props.get("device_vfo_frequency")
    )
    if hz is None or not 0 < hz <= 10_000_000_000:
        raise ValueError("Enter a valid signal frequency")
    if decoder_id == "adsb" and hz != 1_090_000_000:
        raise ValueError("ADS-B / Mode S uses 1090 MHz")
    # SDRconnect's existing span selector supports these rates. Preserve a
    # sufficient current rate rather than forcing every decoder to wide IQ.
    bandwidth = spec.default_bandwidth_hz
    if profile_spec:
        bandwidth = profile_spec["decoder_settings"]["lora_bandwidth_hz"]
    if decoder_id == "dsc" and hz >= 30_000_000:
        bandwidth = 12_500
    rate = int(props.get("device_sample_rate") or 0)
    if rate < bandwidth * 1.1:
        rate = next((r for r in (2, 4, 6, 8, 10) if r * 1_000_000 >= bandwidth * 1.1), 0)
        if not rate:
            raise ValueError("No compatible IQ sample rate")
        rate *= 1_000_000
    desired = {"device_sample_rate": rate, "device_center_frequency": hz,
               "device_vfo_frequency": hz}
    notes = ["Antenna is preserved. Receiver readiness does not guarantee decoding."]
    if "IQ" not in spec.demodulators:
        current = props.get("demodulator")
        mode = current if current in spec.demodulators else spec.demodulators[0]
        desired.update(demodulator=mode,
                       filter_bandwidth=bandwidth, squelch_enable=False,
                       noise_reduction_enable=False)
    else:
        notes.append("Raw IQ decoder: listening mode and audio filter do not affect decoding.")
    if lna_state is not None:
        desired["lna_state"] = lna_state
        notes.append("LNA state is device-specific, not gain in dB; watch for overload.")
    missing = set(desired) - props.keys()
    if missing:
        raise ValueError("Receiver settings not synchronized: " + ", ".join(sorted(missing)))
    result = {"decoder": decoder_id, "label": profile_spec["label"] if profile_spec else spec.label,
            "desired": desired,
            "before": {k: props[k] for k in desired}, "notes": notes,
            "changes": [{"property": k, "before": props[k], "after": v}
                        for k, v in desired.items() if props[k] != v]}
    if profile_spec:
        settings = profile_spec["decoder_settings"]
        if decoder_settings is None or any(key not in decoder_settings for key in settings):
            raise ValueError("LoRa decoder settings are not synchronized")
        result["profile"] = profile
        result["decoder_settings_before"] = {k: decoder_settings[k] for k in settings}
        result["decoder_settings_desired"] = dict(settings)
        result["decoder_settings_changes"] = [
            {"property": k, "before": decoder_settings[k], "after": v}
            for k, v in settings.items() if decoder_settings[k] != v
        ]
    return result
