from types import SimpleNamespace

import numpy as np
import pytest

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


@pytest.mark.parametrize("phase_deg", [73.0, 267.0])
def test_vor_recovers_known_radial(phase_deg):
    # VOR standard: the 30 Hz FM on the 9960 Hz subcarrier (+/-480 Hz) is the
    # reference; the 30 Hz AM is the variable signal, lagging it by the
    # radial. 73 deg vs 287 deg would catch a sign flip; 267 deg is the OAK
    # VOR radial seen off-air from a known location (reported as 93 deg).
    rate = 48_000
    t = np.arange(rate) / rate
    subcarrier_phase = 2 * np.pi * 9960 * t + 16 * np.sin(2 * np.pi * 30 * t)
    envelope = (
        1
        + 0.2 * np.cos(2 * np.pi * 30 * t - np.deg2rad(phase_deg))
        + 0.3 * np.cos(subcarrier_phase)
    )
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


def _vor_envelope(rate, am_depth, deviation_hz, radial_deg=120.0, noise=0.0):
    t = np.arange(rate) / rate
    sub = 2 * np.pi * 9960 * t + (deviation_hz / 30) * np.sin(2 * np.pi * 30 * t)
    env = 1 + am_depth * np.cos(2 * np.pi * 30 * t - np.deg2rad(radial_deg)) + 0.3 * np.cos(sub)
    return env + noise * np.random.default_rng(0).normal(size=rate)


@pytest.mark.parametrize(
    ("am_depth", "deviation_hz", "reported"),
    [
        (0.3, 480, True),  # nominal VOR
        (0.3, 90, False),  # subcarrier not locked (off-air SAU showed 18-91 Hz)
        (0.01, 480, False),  # AM tone at noise level
    ],
)
def test_vor_lock_checks_drop_unlocked_blocks(am_depth, deviation_hz, reported):
    import asyncio

    decoder = NavigationIQDecoder(SimpleNamespace(id="vor"), sample_rate_hz=48_000,
                                  center_hz=116.2e6, frequency_hz=116.2e6)
    asyncio.run(decoder.start())
    decoder.feed(_iq_bytes(_vor_envelope(48_000, am_depth, deviation_hz) * 0.5))
    assert decoder._events.empty() is not reported


def test_ils_on_a_vor_channel_reports_nothing():
    # Off-air: the ILS decoder on OAK VOR (116.8) reported ~0.5% "depths".
    import asyncio

    decoder = NavigationIQDecoder(SimpleNamespace(id="ils"), sample_rate_hz=48_000,
                                  center_hz=116.8e6, frequency_hz=116.8e6)
    asyncio.run(decoder.start())
    decoder.feed(_iq_bytes(_vor_envelope(48_000, 0.3, 480) * 0.5))
    assert decoder._events.empty()
