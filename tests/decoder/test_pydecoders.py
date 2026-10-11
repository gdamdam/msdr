"""Synthesised-signal round trips for the S7-S10 in-process (PythonBackend)
decoders: wefax (radiofax) and navtex (SITOR-B/NAVTEX FEC)."""

import struct
import zlib

import numpy as np

from msdr.decoder.pydecoders.navtex import NavtexDecoder, encode_message, words_to_bits
from msdr.decoder.pydecoders.wefax import WefaxDecoder

# -- wefax --------------------------------------------------------------

_FS = 11025
_LPM = 120


def _read_png_gray8(path):
    """Minimal reader for this module's own filter-type-0-only PNG writer
    (msdr.decoder.pydecoders.png) -- enough to verify round trips, not a
    general PNG decoder."""
    with open(path, "rb") as image_file:
        data = image_file.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    idat = b""
    width = height = None
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            idat += chunk
        pos += 8 + length + 4
    raw = zlib.decompress(idat)
    stride = width + 1
    rows = [raw[r * stride + 1 : r * stride + 1 + width] for r in range(height)]
    return width, height, rows


def _synth_wefax_audio(rows: int) -> tuple[np.ndarray, list[float]]:
    """3.5s of 300 Hz start tone, `rows` lines each a constant (gradient by
    row) FM tone at the nominal 120 LPM line rate, then 1.5s of 450 Hz stop
    tone -- one continuous, phase-continuous instantaneous-frequency trace."""
    seconds_per_line = 60.0 / _LPM
    n_start = int(round(3.5 * _FS))
    freqs = [300.0] * n_start
    expected_hz = []
    for r in range(rows):
        f = 1500.0 + (2300.0 - 1500.0) * r / (rows - 1)
        expected_hz.append(f)
        n = int(round(_FS * seconds_per_line))
        freqs.extend([f] * n)
    n_stop = int(round(1.5 * _FS))
    freqs.extend([450.0] * n_stop)
    freqs_arr = np.asarray(freqs, dtype=np.float64)
    phase = np.cumsum(2 * np.pi * freqs_arr / _FS)
    return np.sin(phase).astype(np.float32), expected_hz


def test_wefax_decodes_gradient_image_within_tolerance(tmp_path):
    rows = 40
    audio, expected_hz = _synth_wefax_audio(rows)
    decoder = WefaxDecoder(tmp_path)

    events = []
    chunk = 2000  # exercise batching across many feed() calls, not one shot
    for i in range(0, len(audio), chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    events.extend(decoder.flush())

    assert [e.kind for e in events] == ["image", "image"]
    start_event, finish_event = events
    assert start_event.text == "fax started"
    assert start_event.image_path is not None
    assert finish_event.update_of_path == start_event.image_path
    assert finish_event.fields["lines"] == rows

    width, height, png_rows = _read_png_gray8(start_event.image_path)
    assert width == 1809
    assert height == rows

    for r in range(rows):
        mean_gray = np.frombuffer(png_rows[r], dtype=np.uint8).astype(np.float64).mean()
        expected_gray = (expected_hz[r] - 1500.0) / 800.0 * 255.0
        assert abs(mean_gray - expected_gray) <= 0.10 * 255.0, f"row {r} out of tolerance"


def test_wefax_writes_png_progressively_every_20_lines(tmp_path):
    rows = 25
    audio, _ = _synth_wefax_audio(rows)
    # Split off the trailing stop tone so the video portion alone can be fed
    # first -- otherwise a single feed() call would run start..stop to
    # completion in one go, with nothing "in progress" to observe.
    n_stop = int(round(1.5 * _FS))
    video_part, stop_part = audio[:-n_stop], audio[-n_stop:]

    decoder = WefaxDecoder(tmp_path)
    events = list(decoder.feed(video_part))
    start_event = events[0]

    # One 20-line checkpoint has been written; the 25th line doesn't land on
    # another multiple of 20, so the file isn't at its final size yet.
    _width, height, _rows = _read_png_gray8(start_event.image_path)
    assert height == 20

    decoder.feed(stop_part)
    decoder.flush()
    _width, height, _rows = _read_png_gray8(start_event.image_path)
    assert height == rows


# -- navtex ---------------------------------------------------------------

_BAUD = 100.0
_MARK_HZ = 1615.0
_SPACE_HZ = 1785.0


def _synth_navtex_audio(bits: list[int]) -> np.ndarray:
    samples_per_bit = _FS / _BAUD
    freqs = []
    target = 0.0
    consumed = 0
    for bit in bits:
        target += samples_per_bit
        n = int(round(target)) - consumed
        consumed += n
        freqs.extend([_MARK_HZ if bit else _SPACE_HZ] * n)
    freqs_arr = np.asarray(freqs, dtype=np.float64)
    phase = np.cumsum(2 * np.pi * freqs_arr / _FS)
    return np.sin(phase).astype(np.float32)


_MESSAGE = "ZCZC AB12 TEST MESSAGE FOR NAVTEX NNNN"


def _decode(audio: np.ndarray) -> list:
    decoder = NavtexDecoder()
    events = []
    chunk = 3000
    for i in range(0, len(audio), chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    return events


def test_navtex_decodes_clean_message_exactly():
    words = encode_message(_MESSAGE)
    audio = _synth_navtex_audio(words_to_bits(words))

    events = _decode(audio)
    assert len(events) == 1
    event = events[0]
    assert event.kind == "text"
    assert event.text == "TEST MESSAGE FOR NAVTEX"
    assert event.fields == {"station": "A", "subject": "B", "serial": "12", "errors": 0}


def test_navtex_fec_delay_is_four_characters():
    # Real SITOR-B repeats each character 4 character positions after the DX
    # transmission (280 ms at 100 Bd) -- not the plan's earlier round-1
    # figure of 5 -- so the RX slot at beat k must equal the DX word from
    # exactly 4 beats earlier.
    from msdr.decoder.pydecoders.navtex import _FEC_DELAY

    assert _FEC_DELAY == 4

    words = encode_message("AB", delay=4)
    dx_words, rx_words = words[0::2], words[1::2]
    for k in range(4, len(dx_words)):
        assert rx_words[k] == dx_words[k - 4]


def test_navtex_repairs_one_corrupted_character_via_rx_repetition():
    words = encode_message(_MESSAGE)
    corrupted = list(words)
    # Flip low bits of one DX codeword (even index = DX slot) so its 4-of-7
    # weight check fails; its RX repeat 5 characters later is untouched.
    beat = 6
    corrupted[beat * 2] ^= 0b0000011
    assert bin(corrupted[beat * 2]).count("1") != 4

    audio = _synth_navtex_audio(words_to_bits(corrupted))
    events = _decode(audio)

    assert len(events) == 1
    event = events[0]
    assert event.text == "TEST MESSAGE FOR NAVTEX"  # repaired, byte-for-byte
    assert event.fields["errors"] == 0  # RX repetition covered it, not counted as an error


def test_rs41_resampler_does_not_mutate_callers_iq():
    import numpy as np

    from msdr.decoder.pydecoders.rs41 import _StreamingResampler

    resampler = _StreamingResampler(115_200, 5_000.0)
    iq = (np.random.default_rng(0).standard_normal(4096) * (1 + 1j)).astype(np.complex64)
    before = iq.copy()
    resampler.process(iq)
    assert np.array_equal(iq, before)
