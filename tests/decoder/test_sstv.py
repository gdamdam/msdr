"""SSTV decoder round trips: tests/decoder/sstv_encoder.py synthesises PCM
from an RGB image with its own copy of the published timings, the decoder
under test turns it back into a PNG, and the two are compared pixel-wise."""

import importlib
import struct
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from msdr.decoder.pydecoders import sstv as sstv_mod
from msdr.decoder.pydecoders.sstv import SstvDecoder
from msdr.decoder.registry import DECODERS
from tests.decoder import sstv_encoder as enc

_FS = 11025
_CHUNK = 1102  # ~100 ms, PythonBackend's batch size
_NOISE_SIGMA = 0.063  # ~15 dB SNR against the 0.5-amplitude tone over 0..5.5 kHz
_DATA_DIR = Path(__file__).parent / "data"


def _read_png_rgb8(path) -> np.ndarray:
    """Reader for msdr.decoder.pydecoders.png's filter-0 RGB output only."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width, height = 8, b"", None, None
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, _depth, color_type = struct.unpack(">IIBB", chunk[:10])
            assert (_depth, color_type) == (8, 2)
        elif tag == b"IDAT":
            idat += chunk
        pos += 8 + length + 4
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(height, 3 * width + 1)
    assert not raw[:, 0].any(), "filter type 0 expected on every scanline"
    return raw[:, 1:].reshape(height, width, 3)


def _run(decoder: SstvDecoder, audio: np.ndarray, chunk: int = _CHUNK):
    events = []
    for i in range(0, audio.size, chunk):
        events.extend(decoder.feed(audio[i : i + chunk]))
    events.extend(decoder.flush())
    return events


def _with_noise(audio: np.ndarray, sigma: float = _NOISE_SIGMA, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (audio + rng.normal(0.0, sigma, audio.size)).astype(np.float32)


def _block_means(img: np.ndarray, size: int = 8) -> np.ndarray:
    h, w = (img.shape[0] // size) * size, (img.shape[1] // size) * size
    blocks = img[:h, :w].astype(np.float64).reshape(h // size, size, w // size, size, 3)
    return blocks.mean(axis=(1, 3))


# -- VIS -----------------------------------------------------------------------

_MODE_NAMES = {
    "robot36": "Robot 36",
    "robot72": "Robot 72",
    "martin1": "Martin M1",
    "scottie1": "Scottie S1",
    "pd120": "PD 120",
}


@pytest.mark.parametrize("mode", ["robot36", "martin1", "scottie1", "pd120"])
def test_vis_detection_emits_mode_started_event(tmp_path, mode):
    vis, width, height = enc.MODES[mode]
    # Header + VIS, then a second of black video-ish tone: enough to detect,
    # not enough to finish.
    segs = enc.vis_segments(vis) + [(1000.0, 1500.0)]
    audio = _with_noise(enc.synthesize(segs, _FS))
    events = _run(SstvDecoder(tmp_path), audio)
    started = [e for e in events if e.text.endswith("started")]
    assert len(started) == 1
    ev = started[0]
    assert ev.kind == "image"
    assert ev.decoder == "sstv"
    assert ev.fields["mode"] == _MODE_NAMES[mode]
    assert ev.fields["vis"] == vis
    assert (ev.fields["width"], ev.fields["height"]) == (width, height)
    assert ev.image_path and Path(ev.image_path).parent == tmp_path
    assert ev.integrity == "not_applicable"
    assert abs(ev.fields["tuning_offset_hz"]) < 10


def test_vis_parity_failure_is_rejected(tmp_path):
    vis = enc.MODES["robot36"][0]
    segs = enc.vis_segments(vis, parity_ok=False) + [(1000.0, 1500.0)]
    audio = _with_noise(enc.synthesize(segs, _FS))
    assert _run(SstvDecoder(tmp_path), audio) == []


def test_vis_tolerates_tuning_offset(tmp_path):
    """A 60 Hz USB dial error shifts every tone; the leader measures it."""
    vis, width, height = enc.MODES["robot36"]
    segs = [(d, f + 60.0) for d, f in enc.vis_segments(vis)] + [(1000.0, 1560.0)]
    events = _run(SstvDecoder(tmp_path), enc.synthesize(segs, _FS))
    assert events and events[0].fields["mode"] == "Robot 36"
    assert abs(events[0].fields["tuning_offset_hz"] - 60.0) < 8


def test_vis_table_matches_published_codes():
    expected = {
        "Robot 36": 8,
        "Robot 72": 12,
        "Martin M1": 44,
        "Martin M2": 40,
        "Scottie S1": 60,
        "Scottie S2": 56,
        "Scottie DX": 76,
        "PD 50": 93,
        "PD 90": 99,
        "PD 120": 95,
        "PD 180": 96,
        "Wraase SC2-180": 55,
    }
    assert {m.name: m.vis for m in sstv_mod.MODES_BY_NAME.values()} == expected
    for mode in sstv_mod.MODES_BY_NAME.values():
        assert mode.height % mode.lines_per_frame == 0
        assert mode.max_offset_ms <= mode.line_ms + 1e-9


# -- picture round trips ---------------------------------------------------------

# (mode, max whole-image MAE, max |8x8 block mean error| at sampled points).
# YCrCb modes carry chroma on 1.5-2 samples per pixel at 11,025 Hz, so noise
# hits them ~2x harder than the RGB modes.
_ROUND_TRIPS = [
    ("robot36", 18.0, 24.0),
    ("robot72", 16.0, 24.0),
    ("martin1", 9.0, 16.0),
    ("scottie1", 9.0, 16.0),
    ("pd120", 16.0, 20.0),
]


def _sample_points(width: int, height: int):
    """Block centres well inside each colour bar, the grey ramp and the stripe."""
    split = int(height * 0.6) // 2 * 2
    third = (height - split) // 3 // 2 * 2
    rows = [split // 2, split + third // 2, split + third + third // 2]
    bar_cols = [int((k + 0.5) * width / 8) for k in range(8)]
    pts = [(rows[0], c) for c in bar_cols]
    pts += [(r, c) for r in rows[1:] for c in (width // 6, width // 2, 5 * width // 6)]
    return pts


@pytest.mark.parametrize(("mode", "max_mae", "max_point_err"), _ROUND_TRIPS)
def test_decodes_synthesised_image_with_noise(tmp_path, mode, max_mae, max_point_err):
    _vis, width, height = enc.MODES[mode]
    source = enc.test_image(width, height)
    audio = _with_noise(enc.encode(mode, source))
    events = _run(SstvDecoder(tmp_path), audio)

    assert events[0].text == f"SSTV {_MODE_NAMES[mode]} started"
    finish = events[-1]
    assert finish.update_of_path == events[0].image_path
    assert finish.fields["complete"] is True
    assert finish.fields["lines"] == height
    assert finish.fields["reason"] == "complete"
    # Progress updates in between all point at the same image.
    assert all(e.update_of_path == events[0].image_path for e in events[1:])

    got = _read_png_rgb8(events[0].image_path)
    assert got.shape == source.shape
    mae = np.abs(got.astype(np.float64) - source).mean()
    assert mae < max_mae, f"{mode}: MAE {mae:.1f}"

    got_blocks, src_blocks = _block_means(got), _block_means(source)
    for r, c in _sample_points(width, height):
        diff = np.abs(got_blocks[r // 8, c // 8] - src_blocks[r // 8, c // 8]).max()
        assert diff <= max_point_err, f"{mode}: point ({r},{c}) off by {diff:.1f}"


def test_clock_offset_is_tracked_straight(tmp_path):
    """Encoder clock +0.1 % (lines 0.15 ms long): untracked, the picture
    would skew ~130 px top to bottom; sync tracking keeps both edges put."""
    _vis, width, height = enc.MODES["robot36"]
    source = enc.test_image(width, height)
    audio = _with_noise(enc.encode("robot36", source, fs=_FS * 1.001))
    events = _run(SstvDecoder(tmp_path), audio)
    finish = events[-1]
    assert finish.fields["complete"] is True
    assert 800 < finish.fields["slant_ppm"] < 1200
    got = _read_png_rgb8(events[0].image_path).astype(np.float64)
    split = int(height * 0.6) // 2 * 2
    for rows in (slice(4, 24), slice(split - 24, split - 4)):  # top and bottom of the bars
        left = got[rows, 6:14].mean(axis=(0, 1))
        right = got[rows, width - 14 : width - 6].mean(axis=(0, 1))
        assert np.all(left > 225), left  # white bar
        assert np.all(right < 30), right  # black bar


def test_dropout_yields_partial_image(tmp_path):
    _vis, width, height = enc.MODES["robot36"]
    source = enc.test_image(width, height)
    audio = enc.encode("robot36", source, tail_s=0.0)
    header_s = 0.5 + 0.610 + 0.300
    keep = int((header_s + 100 * 0.150) * _FS)  # 100 full lines, then nothing
    truncated = np.concatenate([audio[:keep], np.zeros(int(4.0 * _FS), dtype=np.float32)])
    events = _run(SstvDecoder(tmp_path), _with_noise(truncated, sigma=0.01))
    finish = events[-1]
    assert finish.fields["complete"] is False
    assert finish.fields["reason"] == "signal lost"
    assert finish.text.endswith("(partial)")
    assert 96 <= finish.fields["lines"] <= 102
    got = _read_png_rgb8(events[0].image_path)
    assert got.shape[0] == finish.fields["lines"]
    assert np.abs(got[:96].astype(np.float64) - source[:96]).mean() < 8.0


def test_two_pictures_back_to_back(tmp_path):
    """After a picture finishes the decoder is idle again and catches the
    next header without being recreated."""
    _vis, width, height = enc.MODES["robot36"]
    source = enc.test_image(width, height)
    one = enc.encode("robot36", source, lead_in_s=0.3, tail_s=0.3)
    events = _run(SstvDecoder(tmp_path), np.concatenate([one, one]))
    started = [e for e in events if e.text.endswith("started")]
    finished = [e for e in events if e.fields.get("reason") == "complete"]
    assert len(started) == 2 and len(finished) == 2
    assert started[0].image_path != started[1].image_path


def test_png_is_written_progressively(tmp_path):
    _vis, width, height = enc.MODES["robot36"]
    audio = enc.encode("robot36", enc.test_image(width, height), tail_s=0.0)
    decoder = SstvDecoder(tmp_path)
    half = audio.size // 2
    events = []
    for i in range(0, half, _CHUNK):  # no flush: the picture is still in progress
        events.extend(decoder.feed(audio[i : i + _CHUNK]))
    start = events[0]
    partial = _read_png_rgb8(start.image_path)
    assert 60 < partial.shape[0] < height
    assert partial.shape[1] == width
    progress = [e for e in events[1:] if e.update_of_path == start.image_path]
    assert progress and progress[-1].fields["lines"] == partial.shape[0]


def test_registry_entry_and_python_classes_import():
    spec = DECODERS["sstv"]
    assert spec.kind == "image"
    assert spec.backend == "python"
    assert spec.demodulators == ("USB", "NFM")
    assert spec.default_bandwidth_hz == 3000
    assert spec.input_rate == SstvDecoder.input_rate == 11025
    assert spec.binaries == ()
    assert spec.args == ("msdr.decoder.pydecoders.sstv:SstvDecoder",)
    for other in DECODERS.values():
        if other.backend != "python":
            continue
        module_path, _, class_name = other.args[0].partition(":")
        cls = getattr(importlib.import_module(module_path), class_name)
        assert cls.input_rate == other.input_rate, other.id


def test_decode_is_much_faster_than_real_time(tmp_path):
    _vis, width, height = enc.MODES["robot36"]
    audio = _with_noise(enc.encode("robot36", enc.test_image(width, height)))
    assert audio.size / _FS > 36.0
    decoder = SstvDecoder(tmp_path)
    t0 = time.perf_counter()
    events = _run(decoder, audio)
    elapsed = time.perf_counter() - t0
    assert events[-1].fields["complete"] is True
    assert elapsed < 8.0, f"decode took {elapsed:.1f}s for {audio.size / _FS:.0f}s of audio"


# Third-party-encoded Martin M1 picture (QSSTV, CC BY 2.5 -- see the
# .LICENSE.txt beside it): an independent check of the Martin timings, the
# VIS decoder and sync tracking on audio msdr did not synthesise itself. No
# clearly-licensed off-air Robot 36 recording was found to bundle.
_REAL_WAV = _DATA_DIR / "martin_m1_qsstv_sunset.wav"


def test_third_party_martin_m1_recording_decodes(tmp_path):
    import wave

    with wave.open(str(_REAL_WAV)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 1, _FS)
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.uint8)
    audio = ((pcm.astype(np.float32) - 128.0) / 128.0).astype(np.float32)
    events = _run(SstvDecoder(tmp_path), audio)
    assert events[0].fields["mode"] == "Martin M1"
    assert events[0].fields["vis"] == 44
    finish = events[-1]
    assert finish.fields["complete"] is True
    assert finish.fields["lines"] == 256
    # QSSTV's clock vs. the Ogg/resample chain: a few hundred ppm, tracked.
    assert abs(finish.fields["slant_ppm"]) < 600
    got = _read_png_rgb8(events[0].image_path).astype(np.float64)
    assert got.shape == (256, 320, 3)
    # Plausible photograph: mid-range mean, real contrast, and strongly
    # correlated adjacent rows (a lost sync lock scrambles them).
    assert 60 < got.mean() < 200 and got.std() > 30
    rows = got.mean(axis=2)
    assert np.corrcoef(rows[:-1].ravel(), rows[1:].ravel())[0, 1] > 0.95
