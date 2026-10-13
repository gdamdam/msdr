import asyncio
import shutil
import sys

import numpy as np
import pytest

from msdr.decoder.backends import DecoderError, build_backend
from msdr.decoder.external_decoders import (
    ExternalIQBackend,
    FreeDVState,
    IQChannelizer,
    build_external_iq_backend,
    parse_rtl433_line,
    parse_sonde_json,
)
from msdr.decoder.registry import DECODERS

BINS = {
    "rtl_433": "/x/rtl_433",
    "freedv_rx": "/x/freedv_rx",
    "dfm09mod": "/x/dfm09mod",
    "m10mod": "/x/m10mod",
}


def test_rtl433_line_keeps_measurements():
    line = (
        '{"time":1760000000,"model":"Acurite-Tower","id":1234,"channel":"A",'
        '"battery_ok":1,"temperature_C":21.5,"humidity":45,"mic":"CHECKSUM","rssi":-7.1}'
    )
    ev = parse_rtl433_line(line, "rtl433")
    assert ev.fields["model"] == "Acurite-Tower" and ev.fields["id"] == 1234
    assert ev.fields["temperature_C"] == 21.5 and ev.fields["humidity"] == 45
    assert ev.fields["channel"] == "A" and ev.fields["sensor_time"] == 1760000000
    assert "21.5 C" in ev.text and "Acurite-Tower 1234" in ev.text
    assert ev.integrity == "unavailable"


@pytest.mark.parametrize("line", ["", "not json", "{}", '{"id": 3}', "[1]"])
def test_rtl433_ignores_non_sensor_lines(line):
    assert parse_rtl433_line(line, "rtl433") is None


def test_sonde_json_maps_like_rs41():
    line = (
        '{ "type": "DFM", "frame": 1234, "id": "DFM-21512345", '
        '"datetime": "2026-10-13T12:00:01.000Z", "lat": 52.1, "lon": 1.25, "alt": 12345.5, '
        '"vel_h": 10.2, "heading": 181.0, "vel_v": 5.1, "sats": 9, "batt": 2.7, "temp": -50.1 }'
    )
    ev = parse_sonde_json(line, "sonde_dfm")
    f = ev.fields
    assert f["serial"] == "DFM-21512345" and f["lat"] == 52.1 and f["lon"] == 1.25
    assert f["altitude_m"] == 12345.5 and f["heading_deg"] == 181.0
    assert f["frame_number"] == 1234 and f["battery_v"] == 2.7 and f["temperature_c"] == -50.1


def test_sonde_without_fix_has_no_position():
    line = '{"type":"RS92","id":"R1234567","frame":3,"lat":0.0,"lon":0.0}'
    ev = parse_sonde_json(line, "sonde_rs92")
    assert ev is not None and "lat" not in ev.fields and "altitude_m" not in ev.fields


def test_sonde_ignores_noise():
    assert parse_sonde_json("", "sonde_m10") is None
    assert parse_sonde_json('{"frame": 1}', "sonde_m10") is None


def test_freedv_sync_transitions_and_text():
    st = FreeDVState("freedv700d", "700D")
    stats = "frame: {}  sync: {}  nin: 960 snr: {} dB  bit errors: 0 clock_off: 0.00 foff: 1.00"
    assert st.parse_stderr(stats.format(1, 0, "-3.00")) is None  # idle at start
    up = st.parse_stderr(stats.format(2, 1, "4.50"))
    assert up.fields["sync"] is True and up.fields["snr_db"] == 4.5 and "acquired" in up.text
    assert st.parse_stderr(stats.format(3, 1, "4.60")) is None  # throttled
    assert "lost" in st.parse_stderr(stats.format(4, 0, "-1.00")).text
    assert st.parse_stderr("BER......: 0.1") is None
    ev = st.parse_text("K6ABC\n")
    assert ev.fields["received_text"] == "K6ABC"
    assert st.parse_text("frames decoded: 40") is None


def test_missing_binaries_raise_with_install_hint_available():
    for did in ("rtl433", "rtl433_wmbus", "freedv700d", "sonde_dfm", "sonde_rs92"):
        assert DECODERS[did].install_hint
    assert DECODERS["rtl433"].install_hint == "brew install rtl_433"
    with pytest.raises(DecoderError, match="required binary 'rtl_433' not found"):
        build_external_iq_backend(
            DECODERS["rtl433"], {}, sample_rate_hz=2_000_000, center_hz=433.9e6,
            frequency_hz=433.92e6,
        )
    with pytest.raises(DecoderError, match="freedv_rx"):
        build_backend(DECODERS["freedv700d"], {})


def test_rtl433_argv_and_wmbus_rate():
    b = build_external_iq_backend(
        DECODERS["rtl433"], BINS, sample_rate_hz=2_000_000, center_hz=433.9e6, frequency_hz=433.92e6
    )
    argv = b._argv
    assert argv[:3] == ["/x/rtl_433", "-r", "cs16:-"]
    assert argv[argv.index("-s") + 1] == "250000" and argv[argv.index("-F") + 1] == "json"
    assert argv[argv.index("-f") + 1] == "433920000"
    w = build_external_iq_backend(
        DECODERS["rtl433_wmbus"], BINS, sample_rate_hz=2_000_000, center_hz=868.3e6,
        frequency_hz=868.3e6,
    )
    assert w._argv[w._argv.index("-s") + 1] == "1000000" and "238" in w._argv


def test_sonde_argv_reads_iq_wav_from_stdin():
    b = build_external_iq_backend(
        DECODERS["sonde_m10"], BINS, sample_rate_hz=2_000_000, center_hz=404.9e6,
        frequency_hz=405.0e6,
    )
    assert b._argv == ["/x/m10mod", "--json", "--ptu", "--iq0", "-"]
    assert b._preamble[:4] == b"RIFF" and b._preamble[22:24] == (2).to_bytes(2, "little")
    with pytest.raises(DecoderError):
        build_external_iq_backend(
            DECODERS["sonde_m10"], BINS, sample_rate_hz=2_000_000, center_hz=404.9e6,
            frequency_hz=405.0e6 + 2e6,
        )


def test_freedv_argv_by_mode():
    a = build_backend(DECODERS["freedv700d"], BINS)
    assert a._argv[:3] == ["/x/freedv_rx", "-v", "--reliabletext"]
    assert a._argv[-3:-1] == ["700D", "-"]
    b = build_backend(DECODERS["freedv1600"], BINS)
    assert "--reliabletext" not in b._argv and b._argv[-3] == "1600"
    for x in (a, b):
        x._text_path.unlink()


def test_channelizer_moves_offset_tone_to_dc_at_target_rate():
    rate, off = 2_400_000, 100_000
    n = np.arange(rate // 10)
    iq = 0.5 * np.exp(2j * np.pi * off * n / rate)
    raw = np.empty((len(n), 2), "<i2")
    raw[:, 0], raw[:, 1] = iq.real * 32767, iq.imag * 32767
    ch = IQChannelizer(rate, off, 250_000)
    out = ch.process(raw.tobytes())
    assert abs(len(out) - 25_000) < 200
    settled = out[len(out) // 2 :]
    assert abs(np.mean(settled)) > 0.45 and np.std(np.angle(settled)) < 0.05


async def test_external_iq_backend_pipes_resampled_iq_to_child():
    script = (
        "import sys;n=0\n"
        "while n<=3: n+=len(sys.stdin.buffer.read1(1<<20))\n"
        "print('{\"model\":\"Echo\",\"id\":%d}' % n,flush=True)"
    )
    backend = ExternalIQBackend(
        [sys.executable, "-c", script],
        lambda line: parse_rtl433_line(line, "rtl433"),
        decoder_id="rtl433",
        sample_rate_hz=1_000_000,
        center_hz=0,
        frequency_hz=0,
        out_rate=250_000,
        preamble=b"HDR",
    )
    await backend.start()
    # Fed from a worker thread, as IQDecoderSession does.
    await asyncio.to_thread(backend.feed, np.zeros(40_000, "<i2").tobytes())
    ev = await asyncio.wait_for(backend.events().__anext__(), 3)
    await backend.stop()
    assert ev.fields["model"] == "Echo" and ev.fields["id"] > 3


def _cs16(iq):
    raw = np.empty((len(iq), 2), "<i2")
    raw[:, 0], raw[:, 1] = iq.real * 32767, iq.imag * 32767
    return raw.tobytes()


def test_channelizer_attenuates_tone_just_outside_band_by_40_db():
    rate, out = 2_400_000, 250_000
    n = np.arange(rate)

    def level(freq):
        iq = 0.5 * np.exp(2j * np.pi * freq * n / rate)
        y = IQChannelizer(rate, 0, out).process(_cs16(iq))
        return np.sqrt(np.mean(np.abs(y[len(y) // 3 :]) ** 2))

    in_band, outside = level(50_000), level(0.6 * out)  # 150 kHz > 0.5 * 250 kS/s
    assert 20 * np.log10(in_band / outside) >= 40


RTL433 = shutil.which("rtl_433")


@pytest.mark.skipif(RTL433 is None, reason="rtl_433 not installed")
async def test_real_rtl433_accepts_noise_and_exits_cleanly():
    spec = DECODERS["rtl433"]
    backend = build_external_iq_backend(
        spec, {"rtl_433": RTL433}, sample_rate_hz=1_000_000, center_hz=433.92e6,
        frequency_hz=433.92e6,
    )
    await backend.start()
    noise = (np.random.default_rng(1).normal(0, 0.01, 1_000_000) * 32767).astype("<i2")
    for i in range(0, len(noise), 100_000):
        await asyncio.to_thread(backend.feed, noise[i : i + 100_000].tobytes())
    await asyncio.sleep(0.5)
    await backend.stop()
    assert backend._proc.returncode == 0
    assert backend._events.empty()


def _nexus_iq(fs: int = 250_000) -> bytes:
    """Nexus-TH OOK/PPM burst (id 0x5A, ch 2, 21.5 C, 55 %): 12 repeats of 36 bits."""
    bits = []
    for value, width in ((0x5A, 8), (1, 1), (0, 1), (1, 2), (215, 12), (0xF, 4), (55, 8)):
        bits += [(value >> (width - 1 - i)) & 1 for i in range(width)]

    def us(t):
        return int(t * fs / 1e6)

    sig = [0] * us(20_000)
    for _ in range(12):
        for bit in bits:
            sig += [1] * us(500) + [0] * us(2000 if bit else 1000)
        sig += [1] * us(500) + [0] * us(4000)
    # rtl_433 only flushes the last package once it has seen later samples.
    sig += [0] * us(500_000)
    iq = np.zeros(len(sig), complex)
    iq[:] = 0.5 * np.array(sig) + np.random.default_rng(0).normal(0, 0.0015, len(sig))
    return _cs16(iq)


@pytest.mark.skipif(RTL433 is None, reason="rtl_433 not installed")
async def test_real_rtl433_decodes_synthetic_nexus_through_backend():
    backend = build_external_iq_backend(
        DECODERS["rtl433"], {"rtl_433": RTL433}, sample_rate_hz=250_000, center_hz=433.92e6,
        frequency_hz=433.92e6,
    )
    await backend.start()
    data = _nexus_iq()
    for i in range(0, len(data), 40_000):
        await asyncio.to_thread(backend.feed, data[i : i + 40_000])
    ev = await asyncio.wait_for(backend.events().__anext__(), 5)
    await backend.stop()
    assert ev.fields["model"] == "Nexus-TH" and ev.fields["id"] == 0x5A
    assert ev.fields["temperature_C"] == 21.5 and ev.fields["humidity"] == 55
