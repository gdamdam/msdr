from types import SimpleNamespace

import numpy as np

from msdr.decoder.pydecoders.navigation import NavigationIQDecoder


def _iq_bytes(envelope: np.ndarray) -> bytes:
    iq = np.column_stack((envelope, np.zeros_like(envelope)))
    return np.clip(np.rint(iq * 30000), -32768, 32767).astype("<i2").tobytes()


def test_ils_reports_known_ddm_and_is_unvalidated():
    rate = 48_000
    t = np.arange(rate) / rate
    depth90, depth150 = 0.12, 0.04
    envelope = 1 + depth90 * np.cos(2 * np.pi * 90 * t) + depth150 * np.cos(2 * np.pi * 150 * t)
    decoder = NavigationIQDecoder(SimpleNamespace(id="ils"), sample_rate_hz=rate,
                                  center_hz=110e6, frequency_hz=110e6)
    import asyncio

    asyncio.run(decoder.start())
    decoder.feed(_iq_bytes(envelope))
    event = asyncio.run(decoder.get_event())
    assert event is not None
    assert event.kind == "measurement"
    assert abs(event.fields["ddm"] - 0.08) < 0.004
    assert event.integrity == "unavailable"
    assert event.fields["measurement_status"] == "unvalidated"


def test_vor_recovers_known_variable_phase():
    rate = 48_000
    t = np.arange(rate) / rate
    phase_deg = 73.0
    subcarrier_phase = 2 * np.pi * 9960 * t + 16 * np.sin(
        2 * np.pi * 30 * t - np.deg2rad(phase_deg)
    )
    envelope = 1 + 0.2 * np.cos(2 * np.pi * 30 * t) + 0.3 * np.cos(subcarrier_phase)
    decoder = NavigationIQDecoder(SimpleNamespace(id="vor"), sample_rate_hz=rate,
                                  center_hz=115e6, frequency_hz=115e6)
    import asyncio

    asyncio.run(decoder.start())
    decoder.feed(_iq_bytes(envelope))
    event = asyncio.run(decoder.get_event())
    assert event is not None
    assert event.kind == "measurement"
    error = (event.fields["radial_deg"] - phase_deg + 180) % 360 - 180
    assert abs(error) < 8
    assert event.fields["measurement_status"] == "unvalidated"


def test_empty_iq_does_not_fabricate_measurement():
    decoder = NavigationIQDecoder(SimpleNamespace(id="ils"), sample_rate_hz=48_000,
                                  center_hz=110e6, frequency_hz=110e6)
    import asyncio

    asyncio.run(decoder.start())
    decoder.feed(_iq_bytes(np.zeros(48_000)))
    assert decoder._events.empty()
