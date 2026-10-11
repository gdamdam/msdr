"""Coverage for ADS-B IQ resampling and signed 16-bit decoder input."""

import shutil
import subprocess

import numpy as np
import pytest

from msdr.decoder.standalone_adsb import _TARGET_RATE, _StreamingIQResampler


def _iq_bytes(values):
    raw = np.empty((len(values), 2), dtype="<i2")
    for i, v in enumerate(values):
        raw[i, 0] = int(round(v.real * 32767))
        raw[i, 1] = int(round(v.imag * 32767))
    return raw.tobytes()


def test_matching_rate_bypasses_the_fir_entirely():
    resampler = _StreamingIQResampler(_TARGET_RATE, offset_hz=0.0)
    values = [1 + 0j, 0 + 1j, -1 + 0j, 0 - 1j]
    out = resampler.process(_iq_bytes(values))
    assert len(out) == len(values)
    np.testing.assert_allclose(out, np.array(values, dtype=np.complex64), atol=1e-3)


def test_matching_rate_still_applies_the_frequency_offset():
    resampler = _StreamingIQResampler(_TARGET_RATE, offset_hz=_TARGET_RATE / 4)
    out = resampler.process(_iq_bytes([1 + 0j] * 4))
    # A quarter-rate offset rotates each successive sample by -90 degrees.
    expected = np.exp(-2j * np.pi * np.arange(4) / 4).astype(np.complex64)
    np.testing.assert_allclose(out, expected, atol=1e-3)


def test_mismatched_rate_still_uses_the_polyphase_path():
    resampler = _StreamingIQResampler(_TARGET_RATE * 2, offset_hz=0.0)
    assert not (resampler._step_num == 1 and resampler._step_den == 1)


def test_sc16_preserves_weak_samples_lost_by_uc8():
    values = [0.005 + 0j, -0.005 + 0j, 0 + 0.005j]
    raw = _iq_bytes(values)
    sc16 = np.frombuffer(_StreamingIQResampler(_TARGET_RATE, 0).to_sc16(raw), dtype="<i2")
    uc8 = np.frombuffer(_StreamingIQResampler(_TARGET_RATE, 0).to_uc8(raw), dtype=np.uint8)
    np.testing.assert_array_equal(sc16, np.frombuffer(raw, dtype="<i2"))
    assert len(np.unique(uc8)) <= 3
    assert max(abs(int(v) - 128) for v in uc8) <= 1


def test_matching_rate_sc16_roundtrips_full_scale_samples():
    raw = _iq_bytes([1 + 1j, -1 - 1j])
    out = np.frombuffer(_StreamingIQResampler(_TARGET_RATE, 0).to_sc16(raw), dtype="<i2")
    np.testing.assert_array_equal(out, np.frombuffer(raw, dtype="<i2"))


def test_weak_mode_s_message_survives_six_megasample_resampling(tmp_path):
    binary = shutil.which("dump1090-fa") or shutil.which("dump1090")
    if not binary:
        pytest.skip("dump1090-fa is not installed")

    message = "8D40621D58C382D690C8AC2863A7"
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(message), dtype=np.uint8))
    iq = np.zeros(60_000, dtype=np.complex64)
    for start in (1000, 10000, 20000, 30000, 40000, 50000):
        for offset in (0, 6, 21, 27):
            iq[start + offset : start + offset + 3] = 0.005
        for bit_index, bit in enumerate(bits):
            offset = 0 if bit else 3
            pos = start + 48 + bit_index * 6 + offset
            iq[pos : pos + 3] = 0.005
    raw = _iq_bytes(iq)
    converted = _StreamingIQResampler(6_000_000, 0).to_sc16(raw)
    capture = tmp_path / "weak-adsb.sc16"
    capture.write_bytes(converted)
    result = subprocess.run(
        [binary, "--device-type", "ifile", "--ifile", str(capture), "--iformat", "SC16", "--raw"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert f"*{message};" in result.stdout.upper()


def test_sbs_uses_receipt_time_not_timezone_free_decoder_clock(monkeypatch):
    from msdr.decoder import standalone_adsb

    now = 1790619300.0
    monkeypatch.setattr(standalone_adsb.time, "time", lambda: now)
    columns = ["MSG", "3", "1", "1", "ABC123", "1", "2026/09/28", "11:15:00.000",
               "2026/09/28", "11:15:00.000", "", "5000", "", "", "37.77", "-122.41",
               "", "", "0", "0", "0", "0"]
    event = standalone_adsb.parse_sbs_line(",".join(columns))
    assert event.ts == now
    assert event.fields["logged_time"] == "11:15:00.000"
    assert event.fields["latitude"] == 37.77


async def test_stop_reports_stderr_tail_on_nonzero_exit():
    from msdr.decoder.standalone_adsb import StandaloneADSBBackend

    class FakeProcess:
        returncode = 2
        stdin = None
        stderr = None

        async def wait(self):
            return 2

        def kill(self):
            pass

    backend = StandaloneADSBBackend(
        "/fake/dump1090-fa", sample_rate_hz=2_000_000, center_hz=1_090e6, frequency_hz=1_090e6
    )
    backend._process = FakeProcess()
    backend._stderr_tail.extend(["Error opening the input file", "exiting"])
    await backend.stop()
    assert backend.error == "dump1090-fa exited 2: Error opening the input file\nexiting"
