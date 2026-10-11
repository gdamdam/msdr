"""Test-only SSTV *encoder*: RGB image -> PCM, for round-tripping through
msdr.decoder.pydecoders.sstv.

Timings are written out again here from the published spec (JL Barber N7CXI,
"Proposal for SSTV Mode Specifications", Dayton 2000) on purpose -- nothing
is imported from the decoder's mode table, so a typo there cannot cancel out
against the same typo here.
"""

import numpy as np

SYNC_HZ = 1200.0
BLACK_HZ = 1500.0
WHITE_HZ = 2300.0

# name -> (vis, width, height)
MODES = {
    "robot36": (8, 320, 240),
    "robot72": (12, 320, 240),
    "martin1": (44, 320, 256),
    "scottie1": (60, 320, 256),
    "pd120": (95, 640, 496),
}


def rgb_to_ycrcb(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Barber paper's Robot/PD colour equations (BT.601 studio range)."""
    r = rgb[..., 0].astype(np.float64)
    g = rgb[..., 1].astype(np.float64)
    b = rgb[..., 2].astype(np.float64)
    y = 16.0 + 0.003906 * (65.738 * r + 129.057 * g + 25.064 * b)
    cr = 128.0 + 0.003906 * (112.439 * r - 94.154 * g - 18.285 * b)
    cb = 128.0 + 0.003906 * (-37.945 * r - 74.494 * g + 112.439 * b)
    return y, cr, cb


def _pix(values: np.ndarray) -> np.ndarray:
    return BLACK_HZ + (WHITE_HZ - BLACK_HZ) * np.asarray(values, dtype=np.float64) / 255.0


def vis_segments(code: int, parity_ok: bool = True) -> list[tuple[float, object]]:
    """Calibration header + VIS: (duration_ms, frequency) tone list."""
    bits = [(code >> i) & 1 for i in range(7)]
    parity = sum(bits) % 2
    if not parity_ok:
        parity ^= 1
    bits.append(parity)
    segs: list[tuple[float, object]] = [(300.0, 1900.0), (10.0, 1200.0), (300.0, 1900.0)]
    segs.append((30.0, 1200.0))
    for bit in bits:
        segs.append((30.0, 1100.0 if bit else 1300.0))
    segs.append((30.0, 1200.0))
    return segs


def image_segments(mode: str, rgb: np.ndarray) -> list[tuple[float, object]]:
    """Picture body as (duration_ms, tone_hz | pixel-frequency array) list."""
    _vis, width, height = MODES[mode]
    assert rgb.shape == (height, width, 3), rgb.shape
    segs: list[tuple[float, object]] = []
    if mode == "robot36":
        y, cr, cb = rgb_to_ycrcb(rgb)
        for n in range(height):
            pair = slice(n - (n % 2), n - (n % 2) + 2)
            segs += [(9.0, SYNC_HZ), (3.0, 1500.0), (88.0, _pix(y[n]))]
            if n % 2 == 0:
                segs += [(4.5, 1500.0), (1.5, 1900.0), (44.0, _pix(cr[pair].mean(axis=0)))]
            else:
                segs += [(4.5, 2300.0), (1.5, 1900.0), (44.0, _pix(cb[pair].mean(axis=0)))]
    elif mode == "robot72":
        y, cr, cb = rgb_to_ycrcb(rgb)
        for n in range(height):
            segs += [(9.0, SYNC_HZ), (3.0, 1500.0), (138.0, _pix(y[n]))]
            segs += [(4.5, 1500.0), (1.5, 1900.0), (69.0, _pix(cr[n]))]
            segs += [(4.5, 2300.0), (1.5, 1900.0), (69.0, _pix(cb[n]))]
    elif mode == "martin1":
        for n in range(height):
            segs += [(4.862, SYNC_HZ), (0.572, 1500.0)]
            for ch in (1, 2, 0):  # G, B, R
                segs += [(146.432, _pix(rgb[n, :, ch])), (0.572, 1500.0)]
    elif mode == "scottie1":
        segs.append((9.0, SYNC_HZ))  # first line only
        for n in range(height):
            segs += [(1.5, 1500.0), (138.240, _pix(rgb[n, :, 1]))]
            segs += [(1.5, 1500.0), (138.240, _pix(rgb[n, :, 2]))]
            segs += [(9.0, SYNC_HZ), (1.5, 1500.0), (138.240, _pix(rgb[n, :, 0]))]
    elif mode == "pd120":
        y, cr, cb = rgb_to_ycrcb(rgb)
        for n in range(0, height, 2):
            segs += [(20.0, SYNC_HZ), (2.080, 1500.0)]
            segs += [(121.6, _pix(y[n])), (121.6, _pix(cr[n : n + 2].mean(axis=0)))]
            segs += [(121.6, _pix(cb[n : n + 2].mean(axis=0))), (121.6, _pix(y[n + 1]))]
    else:
        raise ValueError(mode)
    return segs


def synthesize(segments: list[tuple[float, object]], fs: float, amplitude: float = 0.5):
    """Phase-continuous FM of the segment list at sample rate `fs`."""
    durations = np.asarray([d for d, _ in segments], dtype=np.float64) / 1000.0
    bounds = np.concatenate([[0.0], np.cumsum(durations)])
    total = int(np.ceil(bounds[-1] * fs))
    freq = np.empty(total, dtype=np.float64)
    for (dur_ms, tone), t0, t1 in zip(segments, bounds[:-1], bounds[1:], strict=True):
        i0, i1 = int(np.ceil(t0 * fs)), min(int(np.ceil(t1 * fs)), total)
        if i1 <= i0:
            continue
        if np.isscalar(tone):
            freq[i0:i1] = float(tone)
        else:
            arr = np.asarray(tone)
            t = np.arange(i0, i1) / fs - t0
            idx = np.clip((t / (dur_ms / 1000.0) * arr.size).astype(np.int64), 0, arr.size - 1)
            freq[i0:i1] = arr[idx]
    phase = np.cumsum(2 * np.pi * freq / fs)
    return (amplitude * np.sin(phase)).astype(np.float32)


def encode(
    mode: str,
    rgb: np.ndarray,
    fs: float = 11025.0,
    lead_in_s: float = 0.5,
    tail_s: float = 0.6,
    parity_ok: bool = True,
) -> np.ndarray:
    vis, _w, _h = MODES[mode]
    segs = vis_segments(vis, parity_ok) + image_segments(mode, rgb)
    body = synthesize(segs, fs)
    lead = np.zeros(int(lead_in_s * fs), dtype=np.float32)
    tail = np.zeros(int(tail_s * fs), dtype=np.float32)
    return np.concatenate([lead, body, tail])


def test_image(width: int, height: int) -> np.ndarray:
    """Top 60 %: eight colour bars; bottom: grey ramp left->right with a
    red stripe, so slant, channel order and chroma all show in a diff."""
    bars = np.array(
        [
            [255, 255, 255],
            [255, 255, 0],
            [0, 255, 255],
            [0, 255, 0],
            [255, 0, 255],
            [255, 0, 0],
            [0, 0, 255],
            [0, 0, 0],
        ],
        dtype=np.uint8,
    )
    img = np.zeros((height, width, 3), dtype=np.uint8)
    # Region boundaries on even rows: Robot 36 / PD share chroma across a
    # line pair, so a colour change on an odd row is lost by the *format*.
    split = int(height * 0.6) // 2 * 2
    col_bar = np.minimum(np.arange(width) * 8 // width, 7)
    img[:split] = bars[col_bar][None, :, :]
    ramp = (np.arange(width) * 255 // (width - 1)).astype(np.uint8)
    img[split:] = ramp[None, :, None]
    third = (height - split) // 3 // 2 * 2
    stripe = slice(split + third, split + 2 * third)
    img[stripe] = [200, 40, 40]
    return img
