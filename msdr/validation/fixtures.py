"""Synthetic fixture generators for the S4 corpus scorecard
(msdr.validation.scorecard). SYNTHETIC ONLY -- nothing here reflects a field
capture; every array is generated in code, on demand.

Reuses two already-shared generators rather than re-inventing them:
  * tests/helpers/gmsk.py (W1's shared AIS GMSK-burst generator) for the AIS
    case, imported lazily so this module still imports fine before it lands.
  * msdr.survey.rds's own encode_rds_mpx/fm_modulate_chunks/RdsReceiver
    (production code, not a test double) for the WFM+RDS case.

tests/helpers/ax25.py (the S4 plan's W2-owned shared AX.25 frame generator)
did not exist yet when this was written, so the AFSK1200/AX.25 and Morse/CW
generators below are minimal, self-contained implementations instead of a
shared helper -- see the S4 report for this call-out.
"""

from __future__ import annotations

import numpy as np

AUDIO_RATE_HZ = 22_050  # msdr.decoder.registry._P1_INPUT_RATE (aprs/cw)
AIS_RATE_HZ = 48_000  # NFM flat-tap rate msdr.decoder.pydecoders.ais.AisDecoder expects
IQ_RATE_HZ = 200_000  # matches tests/survey/test_analyzer.py's own convention


# --- generic audio/PCM helpers ---------------------------------------------


def s16_pcm(audio: np.ndarray) -> bytes:
    """float32 mono [-1, 1] -> little-endian s16 mono, what every P1 decoder
    backend (ProcessBackend.feed) and PythonBackend expect."""
    clamped = np.clip(audio, -1.0, 1.0)
    return (clamped * 32767.0).astype("<i2").tobytes()


def white_noise_audio(
    duration_s: float = 1.0, rate: int = AUDIO_RATE_HZ, seed: int = 0
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (0.2 * rng.standard_normal(int(duration_s * rate))).astype(np.float32)


def steady_tone_audio(
    freq_hz: float = 800.0, duration_s: float = 1.0, rate: int = AUDIO_RATE_HZ
) -> np.ndarray:
    t = np.arange(int(duration_s * rate)) / rate
    return (0.5 * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def clipped_audio(base: np.ndarray, gain: float = 4.0, limit: float = 0.3) -> np.ndarray:
    """Hard-limited (clipped) version of any audio array -- the "clipped IQ
    (hard-limited)" negative control, applied at audio rate for the decoder
    checks (the analyzer-facing IQ version is clipped_iq below)."""
    return np.clip(base * gain, -limit, limit).astype(np.float32)


def dropped_blocks_audio(base: np.ndarray, block: int = 512, drop_every: int = 4) -> np.ndarray:
    """Zeroes one block out of every `drop_every` -- the "dropped blocks"
    negative control."""
    out = base.copy()
    for i in range(0, len(out), block * drop_every):
        out[i : i + block] = 0.0
    return out


# --- IQ helpers (analyzer-facing detector cases) ----------------------------


def _carrier_iq(
    envelope: np.ndarray, *, freq_hz: float = 0.0, rate: int = IQ_RATE_HZ
) -> np.ndarray:
    t = np.arange(len(envelope)) / rate
    return (0.5 * envelope * np.exp(1j * 2 * np.pi * freq_hz * t)).astype(np.complex64)


def white_noise_iq(duration_s: float = 1.0, rate: int = IQ_RATE_HZ, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(duration_s * rate)
    return (
        0.1 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    ).astype(np.complex64)


def steady_tone_iq(
    duration_s: float = 1.0, rate: int = IQ_RATE_HZ, freq_hz: float = 0.0
) -> np.ndarray:
    """A bare, always-on carrier -- never keyed, never modulated. The
    "single steady tone" negative control: nothing here should read as a
    protocol, CW, or voice."""
    envelope = np.ones(int(duration_s * rate), dtype=np.float32)
    return _carrier_iq(envelope, freq_hz=freq_hz, rate=rate)


def clipped_iq(iq: np.ndarray, gain: float = 6.0, limit: float = 0.4) -> np.ndarray:
    scaled = iq * gain
    real = np.clip(scaled.real, -limit, limit)
    imag = np.clip(scaled.imag, -limit, limit)
    return (real + 1j * imag).astype(np.complex64)


def dropped_blocks_iq(iq: np.ndarray, block: int = 2048, drop_every: int = 4) -> np.ndarray:
    out = iq.copy()
    for i in range(0, len(out), block * drop_every):
        out[i : i + block] = 0.0
    return out


def adjacent_strong_channel_iq(
    duration_s: float = 1.0,
    rate: int = IQ_RATE_HZ,
    *,
    target_hz: float = 0.0,
    blocker_hz: float = 50_000.0,
    blocker_gain: float = 8.0,
    seed: int = 0,
) -> np.ndarray:
    """The target bin is quiet (noise floor only); a strong steady tone sits
    well outside it. A correct channelizer/analyzer must not let the blocker
    leak into the target's own modulation/family read."""
    quiet = white_noise_iq(duration_s, rate, seed=seed)
    blocker = blocker_gain * _carrier_iq(
        np.ones(int(duration_s * rate), dtype=np.float32), freq_hz=blocker_hz, rate=rate
    )
    return (quiet + blocker).astype(np.complex64)


def am_tone_iq(
    duration_s: float = 1.0, rate: int = IQ_RATE_HZ, *, mod_hz: float = 1_000.0, depth: float = 0.7
) -> np.ndarray:
    t = np.arange(int(duration_s * rate)) / rate
    envelope = (1.0 + depth * np.cos(2 * np.pi * mod_hz * t)).astype(np.float32)
    return _carrier_iq(envelope, rate=rate)


def ssb_tone_iq(
    duration_s: float = 1.0, rate: int = IQ_RATE_HZ, *, tone_hz: float = 1_000.0, usb: bool = True
) -> np.ndarray:
    """A single-sideband tone: the analytic signal of a real tone, shifted
    to the upper (usb=True) or lower sideband."""
    t = np.arange(int(duration_s * rate)) / rate
    sign = 1.0 if usb else -1.0
    return (0.5 * np.exp(1j * sign * 2 * np.pi * tone_hz * t)).astype(np.complex64)


def nfm_voice_like_iq(
    duration_s: float = 1.0, rate: int = IQ_RATE_HZ, *, dev_hz: float = 3_000.0, seed: int = 0
) -> np.ndarray:
    """Speech-*shaped* noise (bandpass 300-3000 Hz, no actual phonemes) FM
    modulated at a voice-like deviation -- "NFM voice-like", not real speech."""
    audio_rate = 8_000
    n_audio = int(duration_s * audio_rate)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n_audio)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n_audio, 1 / audio_rate)
    spec[(freqs < 300) | (freqs > 3_000)] = 0
    audio = np.fft.irfft(spec, n_audio).astype(np.float64)
    peak = np.max(np.abs(audio)) or 1.0
    audio /= peak
    t_audio = np.arange(n_audio) / audio_rate
    t_iq = np.arange(int(duration_s * rate)) / rate
    audio_iq = np.interp(t_iq, t_audio, audio)
    phase = 2 * np.pi * dev_hz * np.cumsum(audio_iq) / rate
    return (0.5 * np.exp(1j * phase)).astype(np.complex64)


# --- Morse / keyed carrier (shared envelope) --------------------------------

_MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
}


def morse_envelope(text: str, *, wpm: float = 15.0, rate: int = AUDIO_RATE_HZ) -> np.ndarray:
    """1.0 while the (fictional) key is down, 0.0 while up -- standard PARIS
    timing (dot = 1.2/wpm s, dash = 3 dots, intra-char gap = 1 dot,
    inter-char gap = 3 dots): real Morse dot/dash duration statistics are
    bimodal, unlike the random keyed-carrier envelope below."""
    dot = 1.2 / wpm
    segments: list[tuple[bool, float]] = []
    for ch in text.upper():
        if ch == " ":
            segments.append((False, 7 * dot))
            continue
        code = _MORSE.get(ch)
        if not code:
            continue
        for sym in code:
            segments.append((True, dot if sym == "." else 3 * dot))
            segments.append((False, dot))
        segments.append((False, 2 * dot))  # 1 (already added) + 2 more = 3 dots
    if not segments:
        segments.append((False, dot))
    out = []
    for on, dur in segments:
        n = max(1, int(dur * rate))
        out.append(np.full(n, 1.0 if on else 0.0, dtype=np.float32))
    return np.concatenate(out)


def morse_on_runs_s(text: str = "CQ CQ", *, wpm: float = 15.0) -> list[float]:
    """The on-duration (seconds) of every dot/dash in `text` -- real Morse's
    bimodal statistics, the positive-control input for
    msdr.survey.features.keying_timing."""
    dot = 1.2 / wpm
    runs: list[float] = []
    for ch in text.upper():
        code = _MORSE.get(ch)
        if not code:
            continue
        runs.extend(dot if sym == "." else 3 * dot for sym in code)
    return runs


def keyed_carrier_on_runs_s(
    *, baud: float = 150.0, duration_s: float = 1.5, seed: int = 0
) -> list[float]:
    """On-durations (seconds) of a RANDOM-bit-length on/off keyed carrier --
    the negative-control input for keying_timing (no dot/dash bimodality)."""
    rng = np.random.default_rng(seed)
    bit_len_s = 1.0 / baud
    n_bits = int(duration_s * baud) + 1
    bits = rng.integers(0, 2, n_bits)
    runs: list[float] = []
    run = 0
    for bit in bits:
        if bit:
            run += 1
        elif run:
            runs.append(run * bit_len_s)
            run = 0
    if run:
        runs.append(run * bit_len_s)
    return runs


def morse_cw_audio(
    text: str = "CQ CQ", *, wpm: float = 15.0, tone_hz: float = 700.0, rate: int = AUDIO_RATE_HZ
) -> np.ndarray:
    env = morse_envelope(text, wpm=wpm, rate=rate)
    t = np.arange(len(env)) / rate
    return (0.6 * env * np.sin(2 * np.pi * tone_hz * t)).astype(np.float32)


def cw_iq(
    text: str = "CQ CQ", *, wpm: float = 15.0, rate: int = IQ_RATE_HZ, freq_hz: float = 0.0
) -> np.ndarray:
    """A carrier keyed with real Morse timing -- the positive-control
    counterpart of keyed_carrier_iq below (same on/off idea, proper
    dot/dash statistics instead of a random bitstream)."""
    env = morse_envelope(text, wpm=wpm, rate=rate)
    env = 0.02 + 0.98 * env  # never fully zero: a believable keyed carrier, not silence
    return _carrier_iq(env, freq_hz=freq_hz, rate=rate)


def keyed_carrier_iq(
    *,
    duration_s: float = 1.0,
    rate: int = IQ_RATE_HZ,
    baud: float = 150.0,
    freq_hz: float = 0.0,
    seed: int = 0,
) -> np.ndarray:
    """An on/off keyed carrier with RANDOM bit lengths, not Morse's bimodal
    dot/dash statistics -- the negative control for "keyed carrier is not
    CW". `baud` is deliberately > 100 Bd (families.family_for's existing
    "ook and rate < 100 Bd" CW gate, and the S4-planned bimodality check),
    so this stays "keyed carrier", never "CW", whether or not the S4
    bimodality check has landed yet in msdr.survey.families."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * rate)
    bit_len = max(1, int(rate / baud))
    n_bits = n // bit_len + 1
    bits = rng.integers(0, 2, n_bits)
    envelope = np.repeat(bits, bit_len)[:n].astype(np.float32)
    envelope = 0.02 + 0.98 * envelope
    return _carrier_iq(envelope, freq_hz=freq_hz, rate=rate)


def keyed_carrier_audio(
    *,
    duration_s: float = 1.0,
    rate: int = AUDIO_RATE_HZ,
    baud: float = 150.0,
    tone_hz: float = 700.0,
    seed: int = 0,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(duration_s * rate)
    bit_len = max(1, int(rate / baud))
    n_bits = n // bit_len + 1
    bits = rng.integers(0, 2, n_bits)
    envelope = np.repeat(bits, bit_len)[:n].astype(np.float32)
    t = np.arange(n) / rate
    return (0.6 * envelope * np.sin(2 * np.pi * tone_hz * t)).astype(np.float32)


# --- AX.25 / AFSK1200 (minimal stand-in for tests/helpers/ax25.py) ----------


def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _ax25_addr(callsign: str, ssid: int, *, last: bool) -> bytes:
    call = callsign.upper().ljust(6)[:6]
    out = bytearray((ord(c) << 1) & 0xFF for c in call)
    out.append(0x60 | ((ssid & 0x0F) << 1) | (1 if last else 0))
    return bytes(out)


def build_ax25_ui_frame(
    src: str, dst: str, info: bytes, *, src_ssid: int = 0, dst_ssid: int = 0
) -> bytes:
    """Minimal AX.25 UI frame (dest, then source, no digipeater path):
    addr + control(0x03, UI) + pid(0xF0, no layer 3) + info + FCS
    (CRC-16/X.25, transmitted LSB-first like the rest of the frame)."""
    addr = _ax25_addr(dst, dst_ssid, last=False) + _ax25_addr(src, src_ssid, last=True)
    body = addr + bytes([0x03, 0xF0]) + info
    fcs = crc16_x25(body)
    return body + bytes([fcs & 0xFF, fcs >> 8])


def _bytes_to_bits_lsb(data: bytes) -> list[int]:
    return [(b >> i) & 1 for b in data for i in range(8)]


def _bit_stuff(bits: list[int]) -> list[int]:
    out, ones = [], 0
    for b in bits:
        out.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            out.append(0)
            ones = 0
    return out


def hdlc_bitstream(frame: bytes, *, flags: int = 4) -> list[int]:
    flag = _bytes_to_bits_lsb(bytes([0x7E]))
    return flag * flags + _bit_stuff(_bytes_to_bits_lsb(frame)) + flag * flags


def afsk1200_audio(
    frame: bytes,
    *,
    rate: int = AUDIO_RATE_HZ,
    baud: float = 1200.0,
    mark_hz: float = 1200.0,
    space_hz: float = 2200.0,
) -> np.ndarray:
    """Bell 202 AFSK: NRZI-encode the (already bit-stuffed, flag-delimited)
    HDLC bitstream (AX.25's own convention: 0 = tone transition, 1 = no
    transition), then synthesise continuous-phase 2-FSK audio."""
    bits = hdlc_bitstream(frame)
    level = 1
    nrzi = []
    for b in bits:
        if b == 0:
            level ^= 1
        nrzi.append(level)
    samples_per_bit = rate / baud
    n = int(len(nrzi) * samples_per_bit)
    freq = np.empty(max(n, 1), dtype=np.float64)
    for i, bit in enumerate(nrzi):
        a = int(round(i * samples_per_bit))
        z = int(round((i + 1) * samples_per_bit))
        freq[a:z] = mark_hz if bit else space_hz
    phase = 2 * np.pi * np.cumsum(freq) / rate
    return (0.6 * np.sin(phase)).astype(np.float32)


def aprs_ui_frame_audio(
    text: str = ">msdr synthetic test fixture", *, rate: int = AUDIO_RATE_HZ
) -> np.ndarray:
    frame = build_ax25_ui_frame("N0CALL", "APRS", text.encode("ascii", "replace"))
    return afsk1200_audio(frame, rate=rate)
