"""Native ADS-B/Mode S decoder (msdr/decoder/pydecoders/adsb_phy.py,
msdr/decoder/pydecoders/adsb.py), validated per docs/adsb-native.md:

1. Bit-level truth: published Mode S/ADS-B frames from "The 1090 MHz Riddle"
   (Junzi Sun, MIT-licensed) decode to the published callsign /
   CPR-resolved position / speed+heading, through the full preamble+PPM
   demod pipeline (not just the message-field decoder); CRC-24 validates
   them and rejects single-bit corruption (no error correction -- ported
   from SDRangel, which does not correct ADS-B CRC errors either).
2. Synthetic PPM IQ at 2 MS/s and 2.4 MS/s (msdr's resampling path):
   message success vs SNR, overlapping/close bursts, DF17/18 and DF11.
3. Real-time factor at 2 MS/s, printed and asserted < 0.5.

SYNTHETIC ONLY for (2)/(3): frame-success and RTF numbers here are a
regression bar for this receiver, not a claim of parity with dump1090-fa or
field performance (no off-air ADS-B capture validated yet).
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from msdr.decoder.pydecoders.adsb import NativeADSBBackend
from msdr.decoder.pydecoders.adsb_phy import (
    _SAMPLE_RATE,
    AdsbReceiver,
    _bits_to_int,
    cpr_resolve_global,
    crc24_remainder,
    decode_me,
)

_KNOWN = {
    "identification": "8D4840D6202CC371C32CE0576098",
    "pos_even": "8D40621D58C382D690C8AC2863A7",
    "pos_odd": "8D40621D58C386435CC412692AD6",
    "velocity": "8D485020994409940838175B284F",
}
_CHIP_RATE = 2_000_000  # 2 chips/bit * 1 Mbit/s


def _msg_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def _me_fields(hex_msg: str) -> dict:
    data = bytes.fromhex(hex_msg)
    bits = _msg_bits(data)
    tc = _bits_to_int(bits[32:37])
    return decode_me(tc, bits[32:88])


def _chip_values(data: bytes) -> np.ndarray:
    """16 preamble chips + 2 PPM chips/bit, as a 0/1 float array."""
    preamble = np.zeros(16, dtype=np.float32)
    preamble[[0, 2, 7, 9]] = 1.0
    bits = _msg_bits(data).astype(np.float32)
    msg_chips = np.empty(len(bits) * 2, dtype=np.float32)
    msg_chips[0::2] = bits
    msg_chips[1::2] = 1.0 - bits
    return np.concatenate([preamble, msg_chips])


def _burst_iq(
    hex_msg: str, sample_rate_hz: int = _SAMPLE_RATE, amplitude: float = 1.0
) -> np.ndarray:
    """Rectangular-pulse PPM burst for `hex_msg`, sampled at `sample_rate_hz`
    (not necessarily an integer multiple of the 2 Mchip/s chip rate)."""
    chips = _chip_values(bytes.fromhex(hex_msg))
    n_out = int(np.ceil(len(chips) * sample_rate_hz / _CHIP_RATE))
    t = np.arange(n_out) / sample_rate_hz
    chip_idx = np.clip((t * _CHIP_RATE).astype(np.int64), 0, len(chips) - 1)
    return (chips[chip_idx] * amplitude).astype(np.complex64)


def _add_noise(iq: np.ndarray, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    noise_power = 1.0 / (10 ** (snr_db / 10))
    noise = (rng.normal(0, 1, len(iq)) + 1j * rng.normal(0, 1, len(iq))) * np.sqrt(noise_power / 2)
    return (iq + noise).astype(np.complex64)


def _make_df11(icao: int, ca: int = 5) -> bytes:
    data = bytes([(11 << 3) | (ca & 0x7), (icao >> 16) & 0xFF, (icao >> 8) & 0xFF, icao & 0xFF])
    crc = crc24_remainder(data)
    return data + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])


def _iq_to_sc16(iq: np.ndarray) -> bytes:
    packed = np.empty((len(iq), 2), dtype="<i2")
    packed[:, 0] = np.clip(np.rint(iq.real * 32767.0), -32768, 32767).astype("<i2")
    packed[:, 1] = np.clip(np.rint(iq.imag * 32767.0), -32768, 32767).astype("<i2")
    return packed.tobytes()


# ------------------------------- Gate 1 -------------------------------


def test_crc24_validates_published_frames():
    for name, hexmsg in _KNOWN.items():
        data = bytes.fromhex(hexmsg)
        parity = int.from_bytes(data[11:14], "big")
        assert crc24_remainder(data[:11]) == parity, name


def test_crc24_rejects_single_bit_corruption():
    for name, hexmsg in _KNOWN.items():
        data = bytearray(bytes.fromhex(hexmsg))
        data[6] ^= 0x01  # flip one bit inside the ME field
        parity = int.from_bytes(bytes(data[11:14]), "big")
        assert crc24_remainder(bytes(data[:11])) != parity, name
        # The demod pipeline must not silently "correct" and emit it.
        iq = _burst_iq(bytes(data).hex())
        assert AdsbReceiver().feed(iq) == []


def test_identification_decodes_published_callsign():
    fields = _me_fields(_KNOWN["identification"])
    assert fields["callsign"] == "KLM1023"


def test_airborne_position_resolves_published_cpr():
    even = _me_fields(_KNOWN["pos_even"])
    odd = _me_fields(_KNOWN["pos_odd"])
    assert even["cpr_odd"] is False
    assert odd["cpr_odd"] is True
    lat, lon = cpr_resolve_global(
        even["cpr_lat"], even["cpr_lon"], odd["cpr_lat"], odd["cpr_lon"], odd_is_latest=False
    )
    # Published worked example (mode-s.org / Junzi Sun's ADS-B Decoding
    # Guide): Lat_EVEN = 52.25720214843750.
    assert lat == pytest.approx(52.25720214843750, abs=1e-9)
    assert lon == pytest.approx(3.91937255859375, abs=1e-9)
    assert even["altitude_ft"] == 38000.0
    assert odd["altitude_ft"] == 38000.0


def test_velocity_decodes_published_speed_and_heading():
    fields = _me_fields(_KNOWN["velocity"])
    assert fields["ground_speed_kt"] == pytest.approx(159.2, abs=0.1)
    assert fields["track_deg"] == pytest.approx(182.88, abs=0.1)


def test_known_frames_decode_end_to_end_through_ppm_demod():
    for name, hexmsg in _KNOWN.items():
        iq = np.concatenate(
            [np.zeros(37, dtype=np.complex64), _burst_iq(hexmsg), np.zeros(41, dtype=np.complex64)]
        )
        msgs = AdsbReceiver().feed(iq)
        assert len(msgs) == 1, name
        assert msgs[0].crc_ok, name
        assert f"{msgs[0].icao:06X}" == hexmsg[2:8].upper(), name


# ------------------------------- Gate 2 -------------------------------


#: A frame is sliced one sample/chip (no oversampling average), so unlike
#: AIS/LoRa's matched-filter receivers, per-bit error rate falls off slowly
#: with SNR and a single wrong bit anywhere in 56/112 bits already fails the
#: exact-match CRC (measured: ~0.35% per-bit error rate at 10 dB by this
#: definition -- see docs/adsb-native.md). SNR points are picked accordingly.
_SNR_POINTS_DB = (6, 10, 14, 18, 22, 26)
#: Candidates near the end of a one-shot buffer need the same lookahead
#: margin AdsbReceiver.feed()'s tail retention guarantees in real streaming
#: (_PREAMBLE_CHIPS + _BITS_LONG*2 = 240 samples); a short, isolated test
#: burst needs that much real trailing data to be found at all.
_TAIL_MARGIN = 260


def test_snr_sweep_df17_message_success(capsys):
    rng = np.random.default_rng(7)
    hexmsg = _KNOWN["velocity"]
    icao = int(hexmsg[2:8], 16)
    trials = 100
    results = {}
    for snr_db in _SNR_POINTS_DB:
        hits = 0
        for _ in range(trials):
            lead = int(rng.integers(20, 80))
            trail = int(rng.integers(_TAIL_MARGIN, _TAIL_MARGIN + 60))
            lead_pad = np.zeros(lead, dtype=np.complex64)
            trail_pad = np.zeros(trail, dtype=np.complex64)
            iq = np.concatenate([lead_pad, _burst_iq(hexmsg), trail_pad])
            iq = _add_noise(iq, snr_db, rng)
            msgs = AdsbReceiver().feed(iq)
            hits += any(m.crc_ok and m.icao == icao for m in msgs)
        results[snr_db] = hits / trials
    print(f"ADS-B DF17 message success vs SNR (2 MS/s, {trials} trials): {results}")
    assert results[_SNR_POINTS_DB[-1]] >= 0.9
    assert results[_SNR_POINTS_DB[-1]] >= results[_SNR_POINTS_DB[0]]


def test_snr_sweep_df11_message_success(capsys):
    rng = np.random.default_rng(11)
    icao = 0x3C1234
    hexmsg = _make_df11(icao).hex().upper()
    trials = 100
    results = {}
    for snr_db in _SNR_POINTS_DB:
        hits = 0
        for _ in range(trials):
            lead = int(rng.integers(20, 80))
            trail = int(rng.integers(_TAIL_MARGIN, _TAIL_MARGIN + 60))
            lead_pad = np.zeros(lead, dtype=np.complex64)
            trail_pad = np.zeros(trail, dtype=np.complex64)
            iq = np.concatenate([lead_pad, _burst_iq(hexmsg), trail_pad])
            iq = _add_noise(iq, snr_db, rng)
            msgs = AdsbReceiver().feed(iq)
            hits += any(m.df == 11 and m.icao == icao for m in msgs)
        results[snr_db] = hits / trials
    print(f"ADS-B DF11 message success vs SNR (2 MS/s, {trials} trials): {results}")
    assert results[_SNR_POINTS_DB[-1]] >= 0.9


def test_overlapping_close_bursts_both_decode():
    msg_a = _KNOWN["velocity"]
    msg_b = _KNOWN["pos_even"]
    burst_a = _burst_iq(msg_a)
    burst_b = _burst_iq(msg_b)
    gap = np.zeros(6, dtype=np.complex64)  # close, not literally overlapping bit-for-bit
    iq = np.concatenate(
        [np.zeros(30, dtype=np.complex64), burst_a, gap, burst_b, np.zeros(30, dtype=np.complex64)]
    )
    msgs = AdsbReceiver().feed(iq)
    icaos = {m.icao for m in msgs if m.crc_ok}
    assert int(msg_a[2:8], 16) in icaos
    assert int(msg_b[2:8], 16) in icaos


def test_native_rate_2_4msps_via_resampling_path():
    """Exercises adsb.py's NativeADSBBackend resampler (2.4 MS/s -> 2 MS/s),
    the same role standalone_adsb.py's resampler plays feeding dump1090."""
    hexmsg = _KNOWN["velocity"]
    icao = int(hexmsg[2:8], 16)
    iq = _burst_iq(hexmsg, sample_rate_hz=2_400_000)
    # Enough trailing data at 2.4 MS/s to still clear the resampler's
    # output-side lookahead margin after decimating to 2 MS/s.
    pad = int(_TAIL_MARGIN * 2_400_000 / _SAMPLE_RATE) + 100
    iq = np.concatenate([np.zeros(pad, dtype=np.complex64), iq, np.zeros(pad, dtype=np.complex64)])
    backend = NativeADSBBackend(
        spec=None, sample_rate_hz=2_400_000, center_hz=0.0, frequency_hz=0.0
    )
    backend.feed(_iq_to_sc16(iq))
    events = []
    while not backend._events.empty():
        events.append(backend._events.get_nowait())
    assert backend.error is None
    icaos = {e.fields["icao"] for e in events}
    assert f"{icao:06X}" in icaos
    match = next(e for e in events if e.fields["icao"] == f"{icao:06X}")
    assert match.integrity == "passed"
    assert match.fields["ground_speed_kt"] == pytest.approx(159.2, abs=0.5)


# ------------------------------- Gate 3 -------------------------------


def test_realtime_factor(capsys):
    rng = np.random.default_rng(42)
    duration_s = 1.0
    n = int(_SAMPLE_RATE * duration_s)
    iq = (rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)).astype(np.complex64) * 0.05
    burst = _burst_iq(_KNOWN["velocity"])
    n_bursts = 50  # dense real-world traffic near a busy airport
    for _ in range(n_bursts):
        pos = int(rng.integers(0, n - len(burst)))
        iq[pos : pos + len(burst)] += burst
    receiver = AdsbReceiver()
    start = time.perf_counter()
    receiver.feed(iq)
    elapsed = time.perf_counter() - start
    rtf = elapsed / duration_s
    print(
        f"ADS-B native real-time factor at {_SAMPLE_RATE} sps, {n_bursts} bursts/s: "
        f"{rtf:.3f} ({elapsed:.3f}s to process {duration_s:.3f}s of IQ)"
    )
    assert rtf < 0.5


def _realistic_burst(hexmsg: str, rate: int, amp: float, rng) -> np.ndarray:
    """Band-limited burst with a random sub-sample start, carrier phase and
    up to +-100 kHz offset: built at 60 MS/s and averaged down, so pulse
    edges do not line up with sample edges the way _burst_iq's do."""
    chips = _chip_values(bytes.fromhex(hexmsg))
    hi = 60_000_000
    f = hi // rate
    t = np.arange(int(len(chips) * hi / _CHIP_RATE) + f) / hi - rng.uniform(0, 1 / rate)
    idx = np.floor(t * _CHIP_RATE).astype(int)
    v = np.where((idx >= 0) & (idx < len(chips)), chips[np.clip(idx, 0, len(chips) - 1)], 0.0)
    v = v[: (len(v) // f) * f].reshape(-1, f).mean(1) * amp
    n = np.arange(len(v))
    cfo = rng.uniform(-1e5, 1e5)
    return v * np.exp(1j * (rng.uniform(0, 2 * np.pi) + 2 * np.pi * cfo * n / rate))


def test_six_msps_live_rate_decodes_realistic_bursts_in_real_time():
    # Regression (2026-10-03): at the live 6 MS/s rate the resample-to-4-MS/s
    # path ran at 1.08x real time, so the IQ session dropped most samples and
    # almost nothing decoded. 6 MS/s is now fed directly at 3 samples/chip.
    rng = np.random.default_rng(11)
    rate, noise, count = 6_000_000, 0.004, 30
    amp = noise * np.sqrt(2) * 10 ** (11 / 20)  # ~11 dB per-sample SNR
    parts = []
    for _ in range(count):
        b = _realistic_burst(_KNOWN["velocity"], rate, amp, rng)
        gap = (rng.normal(size=40_000) + 1j * rng.normal(size=40_000)) * noise
        parts += [gap, b + (rng.normal(size=len(b)) + 1j * rng.normal(size=len(b))) * noise]
    raw = _iq_to_sc16(np.concatenate(parts).astype(np.complex64))
    backend = NativeADSBBackend(
        spec=None, sample_rate_hz=rate, center_hz=1_090_010_000.0, frequency_hz=1_090_000_000.0
    )
    assert backend._resampler is None
    chunk = rate // 20 * 4  # 50 ms, like SDRconnect's frames
    start = time.perf_counter()
    for i in range(0, len(raw), chunk):
        backend.feed(raw[i : i + chunk])
    rtf = (time.perf_counter() - start) / (len(raw) / 4 / rate)
    print(f"6 MS/s realistic bursts: {backend._events.qsize()}/{count}, rtf {rtf:.3f}")
    assert backend._events.qsize() >= count - 2
    assert rtf < 0.5


@pytest.mark.parametrize("samples_per_chip", [2, 3, 4])
def test_weak_frame_survives_after_strong_frame(samples_per_chip):
    # A float32 cumulative energy sum loses this weak burst after the
    # strong one, even though both are valid and separated in time.
    strong = np.repeat(
        _chip_values(bytes.fromhex(_KNOWN["identification"])), samples_per_chip
    ) * 0.8
    weak = np.repeat(
        _chip_values(bytes.fromhex(_KNOWN["velocity"])), samples_per_chip
    ) * 0.001
    iq = np.concatenate([strong, np.zeros(280), weak, np.zeros(1000)]).astype(np.complex64)
    backend = NativeADSBBackend(
        None, sample_rate_hz=_SAMPLE_RATE * samples_per_chip, center_hz=0, frequency_hz=0
    )
    backend.feed(_iq_to_sc16(iq))
    events = [backend._events.get_nowait() for _ in range(backend._events.qsize())]
    assert backend.error is None
    assert [event.fields["icao"] for event in events] == ["4840D6", "485020"]


@pytest.mark.parametrize("chunk_size", [None, 137])
def test_resampled_random_phase_bursts_decode(chunk_size):
    rng = np.random.default_rng(123)
    for trial in range(100):
        burst = _realistic_burst(_KNOWN["velocity"], 2_400_000, 0.2, rng)
        iq = np.concatenate([np.zeros(300), burst, np.zeros(600)]).astype(np.complex64)
        raw = _iq_to_sc16(iq)
        backend = NativeADSBBackend(
            None, sample_rate_hz=2_400_000, center_hz=0, frequency_hz=0
        )
        step = chunk_size * 4 if chunk_size else len(raw)
        for start in range(0, len(raw), step):
            backend.feed(raw[start : start + step])
        assert backend.error is None
        assert backend._events.qsize() == 1, trial
        assert backend._events.get_nowait().fields["icao"] == "485020"


@pytest.mark.parametrize("samples_per_chip", [1, 3])
@pytest.mark.parametrize("gap_chips", [0, 10, 100])
@pytest.mark.parametrize("following_df", [11, 17])
def test_short_reply_does_not_suppress_following_frame(
    samples_per_chip, gap_chips, following_df
):
    short = _chip_values(_make_df11(0x3C1234))
    following = _chip_values(
        _make_df11(0x485020) if following_df == 11 else bytes.fromhex(_KNOWN["velocity"])
    )
    chips = np.concatenate([np.zeros(30), short, np.zeros(gap_chips), following, np.zeros(300)])
    iq = np.repeat(chips, samples_per_chip).astype(np.complex64)
    messages = AdsbReceiver(samples_per_chip=samples_per_chip).feed(iq)
    assert [(msg.df, msg.icao) for msg in messages] == [(11, 0x3C1234), (following_df, 0x485020)]
