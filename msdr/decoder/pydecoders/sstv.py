"""SSTV (slow-scan television) decoder -- audio path, in-process PyDecoder.

Signal model (JL Barber N7CXI, "Proposal for SSTV Mode Specifications",
Dayton SSTV forum 2000): a single audio tone whose instantaneous frequency
carries everything -- 1200 Hz sync, 1500 Hz black .. 2300 Hz white video, and
in front of the picture a calibration header (1900 Hz 300 ms, 1200 Hz 10 ms,
1900 Hz 300 ms) followed by the VIS code (1200 Hz start bit, 8 bits of 30 ms
each -- 1100 Hz = 1, 1300 Hz = 0, LSB first, bit 7 even parity -- and a
1200 Hz stop bit).

Pipeline (all numpy, no per-sample Python in the hot path):
 1. complex band-pass FIR (950..2550 Hz) gives the analytic signal directly;
    its sample-to-sample rotation is the instantaneous frequency and its
    running sum an unwrapped phase.
 2. idle: the leader (>=150 ms of 1900 Hz) ending in a 1200 Hz edge is the
    VIS candidate; each 30 ms bit is classified with a 3-bin DFT (1100 /
    1200 / 1300 Hz) over its middle 20 ms, parity must pass, the code must
    be a known mode. The leader's measured frequency also gives the
    receiver's tuning offset, which is subtracted from every tone after.
 3. imaging: every line's sync pulse is searched in a small window around
    its predicted position; the found positions feed a least-squares fit
    of (line -> sample), whose slope is the true line period (slant /
    sample-clock correction) and whose value is each line's start. Pixel
    values are the mean frequency over each pixel's time slot, read off
    the unwrapped phase (phase difference / slot length), so a line of any
    measured length resamples to the nominal width for free.

Mode geometry lives in _MODES; see docs/sstv.md for the table and what is
approximated.
"""

import logging
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from msdr.decoder.backends import DecodeEvent
from msdr.decoder.pydecoders.png import write_rgb8

logger = logging.getLogger(__name__)

_FS = 11025
_BLACK_HZ = 1500.0
_WHITE_HZ = 2300.0
_SYNC_HZ = 1200.0
_LEADER_HZ = 1900.0

_TAPS = 129
_BAND_LO_HZ = 950.0
_BAND_HI_HZ = 2550.0

_LEADER_MIN_S = 0.15
_LEADER_TOL_HZ = 120.0
_MAX_TUNING_OFFSET_HZ = 150.0
_VIS_BIT_S = 0.030
_VIS_LEN_S = 0.300  # start bit + 8 data bits + stop bit
_VIS_SCAN_BOX_S = 0.005

_SYNC_WINDOW_FIRST_MS = 20.0
_SYNC_WINDOW_MS = 4.0
_SYNC_THRESHOLD_HZ = 1350.0  # midpoint of 1200 Hz sync and 1500 Hz porch
_SYNC_AMP_FLOOR = 0.1  # of the amplitude measured on the leader
_FIT_MIN_POINTS = 6
_FIT_MAX_POINTS = 64
_FIT_MAX_SLANT = 0.01  # a fitted period >1 % off nominal is noise, not slant
_FIT_DISAGREE_MS = 1.5
_DROPOUT_S = 2.0
_DROPOUT_MIN_LINES = 3
_WRITE_EVERY_S = 1.5
_IDLE_KEEP_S = 1.0

_INTEGRITY_NOTE = "analogue SSTV image: no integrity check exists (VIS parity checked once)"


@dataclass(frozen=True)
class SstvMode:
    name: str
    vis: int
    width: int
    height: int
    line_ms: float  # sync-to-sync period
    sync_ms: float
    # Offset of the first sync pulse's *start* from the end of the VIS stop
    # bit. 0 for every family except Scottie, whose per-line sync sits
    # between the blue and red scans (the lone 9 ms pulse right after VIS
    # is not used as a reference).
    first_sync_ms: float
    # (channel, start offset from sync start in ms, duration in ms). Scottie
    # offsets are negative for G and B: they precede the line's sync pulse.
    scans: tuple[tuple[str, float, float], ...]
    color: str  # "rgb" | "ycrcb" (chroma every line) | "ycrcb420" (Robot 36)
    lines_per_frame: int = 1  # PD modes carry two image lines per sync
    # Robot 36 only: (offset, duration) of the separator tone whose value
    # (1500 Hz / 2300 Hz) says whether this line carries R-Y or B-Y.
    separator: tuple[float, float] | None = None

    @property
    def frames(self) -> int:
        return self.height // self.lines_per_frame

    @property
    def min_offset_ms(self) -> float:
        return min(0.0, *(o for _c, o, _d in self.scans))

    @property
    def max_offset_ms(self) -> float:
        return max(self.sync_ms, *(o + d for _c, o, d in self.scans))


def _robot(name, vis, y_ms, c_ms, two_chroma):
    porch_y = 9.0 + 3.0
    cr_start = porch_y + y_ms + 4.5 + 1.5
    if not two_chroma:  # Robot 36: one chroma scan per line, alternating
        return SstvMode(
            name,
            vis,
            320,
            240,
            cr_start + c_ms,
            9.0,
            0.0,
            (("Y", porch_y, y_ms), ("C", cr_start, c_ms)),
            "ycrcb420",
            separator=(porch_y + y_ms, 4.5),
        )
    cb_start = cr_start + c_ms + 4.5 + 1.5
    return SstvMode(
        name,
        vis,
        320,
        240,
        cb_start + c_ms,
        9.0,
        0.0,
        (("Y", porch_y, y_ms), ("Cr", cr_start, c_ms), ("Cb", cb_start, c_ms)),
        "ycrcb",
    )


def _martin(name, vis, scan_ms):
    sync, gap = 4.862, 0.572
    g = sync + gap
    b = g + scan_ms + gap
    r = b + scan_ms + gap
    return SstvMode(
        name,
        vis,
        320,
        256,
        r + scan_ms + gap,
        sync,
        0.0,
        (("G", g, scan_ms), ("B", b, scan_ms), ("R", r, scan_ms)),
        "rgb",
    )


def _scottie(name, vis, scan_ms):
    sync, gap = 9.0, 1.5
    # Transmitted order: sep, G, sep, B, sync, porch, R. Measured from the
    # sync pulse, G and B of the same line lie before it, R after.
    return SstvMode(
        name,
        vis,
        320,
        256,
        sync + 3 * gap + 3 * scan_ms,
        sync,
        sync + gap + scan_ms + gap + scan_ms,
        (
            ("G", -(scan_ms + gap + scan_ms), scan_ms),
            ("B", -scan_ms, scan_ms),
            ("R", sync + gap, scan_ms),
        ),
        "rgb",
    )


def _pd(name, vis, scan_ms, width, height):
    sync, porch = 20.0, 2.08
    y0 = sync + porch
    return SstvMode(
        name,
        vis,
        width,
        height,
        y0 + 4 * scan_ms,
        sync,
        0.0,
        (
            ("Y0", y0, scan_ms),
            ("Cr", y0 + scan_ms, scan_ms),
            ("Cb", y0 + 2 * scan_ms, scan_ms),
            ("Y1", y0 + 3 * scan_ms, scan_ms),
        ),
        "ycrcb",
        lines_per_frame=2,
    )


def _wraase(name, vis, scan_ms):
    sync, porch = 5.5225, 0.5
    r = sync + porch
    return SstvMode(
        name,
        vis,
        320,
        256,
        r + 3 * scan_ms,
        sync,
        0.0,
        (("R", r, scan_ms), ("G", r + scan_ms, scan_ms), ("B", r + 2 * scan_ms, scan_ms)),
        "rgb",
    )


_MODES: dict[int, SstvMode] = {
    m.vis: m
    for m in (
        _robot("Robot 36", 8, 88.0, 44.0, two_chroma=False),
        _robot("Robot 72", 12, 138.0, 69.0, two_chroma=True),
        _martin("Martin M1", 44, 146.432),
        _martin("Martin M2", 40, 73.216),
        _scottie("Scottie S1", 60, 138.240),
        _scottie("Scottie S2", 56, 88.064),
        _scottie("Scottie DX", 76, 345.600),
        _pd("PD 50", 93, 91.520, 320, 256),
        _pd("PD 90", 99, 170.240, 320, 256),
        _pd("PD 120", 95, 121.600, 640, 496),
        _pd("PD 180", 96, 183.040, 640, 496),
        _wraase("Wraase SC2-180", 55, 235.0),
    )
}

MODES_BY_NAME: dict[str, SstvMode] = {m.name: m for m in _MODES.values()}


def _design_taps(num_taps: int, fs: float, lo_hz: float, hi_hz: float) -> np.ndarray:
    """Complex band-pass = windowed-sinc low-pass heterodyned to the band
    centre; applied to a real signal it yields the analytic signal of the
    band (the negative-frequency image falls in the stop band)."""
    centre = 0.5 * (lo_hz + hi_hz)
    half_bw = 0.5 * (hi_hz - lo_hz)
    m = (num_taps - 1) // 2
    n = np.arange(-m, m + 1)
    lp = np.sinc(2.0 * half_bw / fs * n) * np.hamming(num_taps)
    lp /= lp.sum()
    return lp * np.exp(2j * np.pi * centre / fs * n)


class _StreamComplexFIR:
    def __init__(self, taps: np.ndarray):
        self._h = taps
        self._state = np.zeros(len(taps) - 1, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.zeros(0, dtype=np.complex128)
        combined = np.concatenate([self._state, x])
        y = np.convolve(combined, self._h, mode="valid")
        self._state = combined[-(len(self._h) - 1) :]
        return y


def ycrcb_to_rgb(y: np.ndarray, cr: np.ndarray, cb: np.ndarray) -> np.ndarray:
    """ITU-R BT.601 studio-range inverse (the Barber paper's Robot/PD colour
    equations); inputs 0..255 floats, output uint8 (..., 3)."""
    r = (298.082 * y + 408.583 * cr) / 256.0 - 222.921
    g = (298.082 * y - 100.291 * cb - 208.120 * cr) / 256.0 + 135.576
    b = (298.082 * y + 516.412 * cb) / 256.0 - 276.836
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def _ffill_rows(values: np.ndarray, fallback: float) -> np.ndarray:
    """Replace all-NaN rows with the previous valid row (or `fallback`)."""
    out = values.copy()
    valid = ~np.isnan(out[:, 0])
    if not valid.any():
        out[:] = fallback
        return out
    idx = np.where(valid, np.arange(len(out)), -1)
    idx = np.maximum.accumulate(idx)
    out = np.where(idx[:, None] >= 0, out[np.maximum(idx, 0)], fallback)
    return out


class _Image:
    """Per-channel float planes (NaN = not yet received) plus the RGB render."""

    def __init__(self, mode: SstvMode):
        self.mode = mode
        shape = (mode.height, mode.width)
        names = ("R", "G", "B") if mode.color == "rgb" else ("Y", "Cr", "Cb")
        self.planes = {n: np.full(shape, np.nan, dtype=np.float32) for n in names}
        self.lines_done = 0

    def put(self, channel: str, line: int, values: np.ndarray) -> None:
        if 0 <= line < self.mode.height:
            self.planes[channel][line] = values

    def render(self, lines: int) -> np.ndarray:
        lines = max(0, min(lines, self.mode.height))
        if self.mode.color == "rgb":
            planes = [np.nan_to_num(self.planes[c][:lines], nan=0.0) for c in ("R", "G", "B")]
            return np.clip(np.stack(planes, axis=-1), 0, 255).astype(np.uint8)
        y = np.nan_to_num(self.planes["Y"][:lines], nan=16.0)
        cr = self.planes["Cr"][:lines]
        cb = self.planes["Cb"][:lines]
        if self.mode.color == "ycrcb420":
            # Robot 36: one chroma plane per line pair; a pair missing its
            # Cr or Cb line (not yet received, misclassified) borrows the
            # previous pair's rather than going grey.
            n_pairs = (lines + 1) // 2
            pad = 2 * n_pairs - lines
            if pad:
                cr = np.concatenate([cr, np.full((pad, cr.shape[1]), np.nan, np.float32)])
                cb = np.concatenate([cb, np.full((pad, cb.shape[1]), np.nan, np.float32)])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN half pairs
                cr_pair = np.nanmean(cr.reshape(n_pairs, 2, -1), axis=1)
                cb_pair = np.nanmean(cb.reshape(n_pairs, 2, -1), axis=1)
            cr = np.repeat(_ffill_rows(cr_pair, 128.0), 2, axis=0)[:lines]
            cb = np.repeat(_ffill_rows(cb_pair, 128.0), 2, axis=0)[:lines]
        else:
            cr = _ffill_rows(cr, 128.0)
            cb = _ffill_rows(cb, 128.0)
        return ycrcb_to_rgb(y, cr, cb)


class SstvDecoder:
    """PyDecoder: SSTV (Robot / Martin / Scottie / PD / Wraase) over an
    11,025 Hz mono channel; one PNG per received picture under `fax_dir`."""

    input_rate = _FS

    def __init__(self, fax_dir: Path):
        self._fax_dir = Path(fax_dir)
        self._fax_dir.mkdir(parents=True, exist_ok=True)
        self._fir = _StreamComplexFIR(_design_taps(_TAPS, _FS, _BAND_LO_HZ, _BAND_HI_HZ))
        self._last_z: complex = 1.0 + 0j
        self._last_phi = 0.0
        # Rolling buffers of derived per-sample quantities; `_base` is the
        # absolute (since-start) index of element 0, so positions found in
        # one feed() stay valid in the next after trimming.
        self._base = 0
        self._z = np.zeros(0, dtype=np.complex128)
        self._phi = np.zeros(0, dtype=np.float64)
        self._f = np.zeros(0, dtype=np.float64)
        self._amp = np.zeros(0, dtype=np.float64)
        self._state = "idle"
        self._vis_scanned_upto = 0
        self._reset_image()

    # -- image-state fields --------------------------------------------------

    def _reset_image(self) -> None:
        self.mode: SstvMode | None = None
        self._image: _Image | None = None
        self._image_path: Path | None = None
        self._offset_hz = 0.0
        self._ref_amp = 0.0
        self._t0 = 0.0  # absolute sample position of the end of the VIS stop bit
        self._frame = 0
        self._sync_frames: list[int] = []
        self._sync_pos: list[float] = []
        self._fit: tuple[float, float] | None = None  # (period, intercept) in samples
        self._last_sync_frame = -1
        self._last_write_frame = 0
        self._started_at = 0.0

    # -- front end -------------------------------------------------------------

    def _append(self, samples: np.ndarray) -> None:
        z = self._fir.process(samples.astype(np.float64))
        z_full = np.concatenate([[self._last_z], z])
        dphi = np.angle(z_full[1:] * np.conj(z_full[:-1]))
        phi = self._last_phi + np.cumsum(dphi)
        self._last_z = complex(z[-1])
        self._last_phi = float(phi[-1])
        self._z = np.concatenate([self._z, z])
        self._phi = np.concatenate([self._phi, phi])
        self._f = np.concatenate([self._f, dphi * (_FS / (2 * np.pi))])
        self._amp = np.concatenate([self._amp, np.abs(z)])

    def _trim(self, keep_from_abs: float) -> None:
        cut = int(keep_from_abs) - self._base
        if cut <= 0:
            return
        cut = min(cut, len(self._phi))
        self._z = self._z[cut:]
        self._phi = self._phi[cut:]
        self._f = self._f[cut:]
        self._amp = self._amp[cut:]
        self._base += cut

    @property
    def _end(self) -> int:
        return self._base + len(self._phi)

    def _phase_at(self, pos_abs: np.ndarray) -> np.ndarray:
        """Unwrapped phase at fractional absolute sample positions."""
        idx = np.asarray(pos_abs, dtype=np.float64) - self._base
        idx = np.clip(idx, 0.0, len(self._phi) - 1.000001)
        i = idx.astype(np.int64)
        frac = idx - i
        return self._phi[i] * (1.0 - frac) + self._phi[i + 1] * frac

    def _mean_freq(self, a: float, b: float) -> float:
        return float((self._phase_at(np.array([b])) - self._phase_at(np.array([a])))[0]) * (
            _FS / (2 * np.pi * (b - a))
        )

    def _slot_freqs(self, edges: np.ndarray) -> np.ndarray:
        """Mean frequency over each consecutive pair of `edges` (absolute
        positions) along the last axis."""
        ph = self._phase_at(edges)
        return np.diff(ph, axis=-1) / np.diff(edges, axis=-1) * (_FS / (2 * np.pi))

    @staticmethod
    def _box(x: np.ndarray, n: int) -> np.ndarray:
        """Centred moving average, same length as x (edges use shorter windows)."""
        n = max(1, int(n))
        if n == 1 or x.size == 0:
            return x
        c = np.concatenate([[0.0], np.cumsum(x)])
        i = np.arange(x.size)
        lo = np.clip(i - n // 2, 0, x.size)
        hi = np.clip(i - n // 2 + n, 0, x.size)
        return (c[hi] - c[lo]) / np.maximum(hi - lo, 1)

    # -- public protocol -----------------------------------------------------

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        self._append(samples)
        events: list[DecodeEvent] = []
        # A finished image drops straight back to idle, so the tail of this
        # batch is still scanned for the next header.
        for _ in range(2):
            if self._state == "idle":
                events.extend(self._scan_vis())
                if self._state == "idle":
                    self._trim(self._end - int(_IDLE_KEEP_S * _FS))
                    break
            if self._state == "imaging":
                events.extend(self._process_lines())
                if self._state == "imaging":
                    break
        return events

    def flush(self) -> list[DecodeEvent]:
        if self._state == "imaging":
            return [self._finish("stopped")]
        return []

    # -- VIS detection -------------------------------------------------------

    def _scan_vis(self) -> list[DecodeEvent]:
        n = len(self._f)
        vis_len = int(_VIS_LEN_S * _FS)
        box = int(_VIS_SCAN_BOX_S * _FS)
        if n < vis_len + int(_LEADER_MIN_S * _FS) + box:
            return []
        fs_s = self._box(self._f, box)
        mask = np.abs(fs_s - _LEADER_HZ) < _LEADER_TOL_HZ
        edges = np.diff(mask.astype(np.int8))
        starts = np.flatnonzero(edges == 1) + 1
        ends = np.flatnonzero(edges == -1) + 1
        if mask[0]:
            starts = np.concatenate([[0], starts])
        # Runs still open at the buffer end are not candidates yet.
        last_evaluable = n - vis_len - box
        for e in ends:
            s = starts[starts < e]
            if s.size == 0:
                continue
            run = e - s[-1]
            if run < int(_LEADER_MIN_S * _FS):
                continue
            e_abs = self._base + int(e)
            if e_abs <= self._vis_scanned_upto or e > last_evaluable:
                continue
            events = self._try_vis(e_abs)
            if events:
                return events
        self._vis_scanned_upto = max(self._vis_scanned_upto, self._base + last_evaluable)
        return []

    def _tone_bin(self, a: float, b: float, tones: tuple[float, ...]) -> int:
        """Index of the strongest of `tones` in z[a:b] (3-bin DFT)."""
        i0 = int(round(a)) - self._base
        i1 = int(round(b)) - self._base
        seg = self._z[max(i0, 0) : max(i1, 0)]
        if seg.size == 0:
            return -1
        k = np.arange(seg.size)
        basis = np.exp(-2j * np.pi * np.asarray(tones)[:, None] * k[None, :] / _FS)
        mags = np.abs(basis @ seg)
        return int(np.argmax(mags))

    def _try_vis(self, edge_abs: int) -> list[DecodeEvent]:
        bit = _VIS_BIT_S * _FS
        offset = self._mean_freq(edge_abs - 0.15 * _FS, edge_abs - 0.01 * _FS) - _LEADER_HZ
        if abs(offset) > _MAX_TUNING_OFFSET_HZ:
            return []
        tones = (1100.0 + offset, 1200.0 + offset, 1300.0 + offset)
        bits = []
        for k in range(10):
            a = edge_abs + k * bit + 0.005 * _FS
            b = edge_abs + k * bit + 0.025 * _FS
            which = self._tone_bin(a, b, tones)
            if k in (0, 9):
                if which != 1:
                    return []
                continue
            if which == 1:  # a 1200 Hz data bit is neither 0 nor 1
                return []
            bits.append(1 if which == 0 else 0)
        if sum(bits) % 2 != 0:
            logger.info("sstv: VIS parity failed (bits %s)", bits)
            return []
        code = sum(b << i for i, b in enumerate(bits[:7]))
        mode = _MODES.get(code)
        if mode is None:
            logger.info("sstv: VIS %d (0x%02x) is not a supported mode", code, code)
            return []
        return [self._begin_image(mode, edge_abs + 10 * bit, offset, edge_abs)]

    def _begin_image(
        self, mode: SstvMode, t0: float, offset: float, leader_end: int
    ) -> DecodeEvent:
        self._reset_image()
        self.mode = mode
        self._image = _Image(mode)
        self._offset_hz = offset
        i0 = max(0, int(leader_end - 0.15 * _FS) - self._base)
        i1 = max(i0 + 1, leader_end - self._base)
        self._ref_amp = float(np.mean(self._amp[i0:i1]))
        self._t0 = t0
        self._state = "imaging"
        self._started_at = time.time()
        self._image_path = self._fax_dir / f"{uuid.uuid4().hex}.png"
        return DecodeEvent(
            ts=self._started_at,
            decoder="sstv",
            kind="image",
            text=f"SSTV {mode.name} started",
            fields={
                "mode": mode.name,
                "vis": mode.vis,
                "width": mode.width,
                "height": mode.height,
                "lines": 0,
                "tuning_offset_hz": round(offset, 1),
                "started_at": self._started_at,
            },
            image_path=str(self._image_path),
            integrity="not_applicable",
            integrity_note=_INTEGRITY_NOTE,
        )

    # -- line decoding -------------------------------------------------------

    def _ms(self, ms: float) -> float:
        return ms * _FS / 1000.0

    def _nominal_period(self) -> float:
        assert self.mode is not None
        return self._ms(self.mode.line_ms)

    def _predict(self, frame: int) -> float:
        assert self.mode is not None
        if self._fit is not None:
            period, intercept = self._fit
            return intercept + period * frame
        if self._sync_pos:
            return self._sync_pos[-1] + (frame - self._sync_frames[-1]) * self._nominal_period()
        return self._t0 + self._ms(self.mode.first_sync_ms) + frame * self._nominal_period()

    def _refit(self) -> None:
        if len(self._sync_pos) < _FIT_MIN_POINTS:
            self._fit = None
            return
        xs = np.asarray(self._sync_frames[-_FIT_MAX_POINTS:], dtype=np.float64)
        ys = np.asarray(self._sync_pos[-_FIT_MAX_POINTS:], dtype=np.float64)
        period, intercept = np.polyfit(xs, ys, 1)
        nominal = self._nominal_period()
        if abs(period / nominal - 1.0) > _FIT_MAX_SLANT:
            self._fit = None
            return
        self._fit = (float(period), float(intercept))

    def _find_sync(self, pred: float, window: float) -> float | None:
        """Sync pulse start whose trailing edge (1200 -> 1500 Hz porch) lies
        within +-window of the predicted one (absolute positions); None if
        no plausible pulse. The leading edge is not used: on the first line
        the 1200 Hz stop bit runs straight into the sync pulse."""
        assert self.mode is not None
        sync_len = self._ms(self.mode.sync_ms)
        box = int(np.clip(sync_len / 4, self._ms(1.0), self._ms(5.0)))
        i0 = int(pred - window) - self._base - box
        i1 = int(pred + sync_len + window) - self._base + box + 1
        if i0 < 0 or i1 > len(self._f):
            return None
        f_s = self._box(self._f[i0:i1], box)
        below = f_s < (_SYNC_THRESHOLD_HZ + self._offset_hz)
        below[:box] = False
        below[-box:] = False
        if not below.any():
            return None
        edges = np.diff(np.concatenate([[0], below.astype(np.int8), [0]]))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        expected_end = pred + sync_len - self._base - i0
        ok = (ends - starts >= 0.5 * sync_len) & (np.abs(ends - expected_end) <= window)
        if not ok.any():
            return None
        candidates = np.flatnonzero(ok)
        best = candidates[np.argmin(np.abs(ends[candidates] - expected_end))]
        run_lo, run_hi = starts[best], ends[best]
        if self._ref_amp > 0 and self._amp[i0 + run_lo : i0 + run_hi].mean() < (
            _SYNC_AMP_FLOOR * self._ref_amp
        ):
            return None
        return self._base + i0 + float(run_hi) - sync_len

    def _process_lines(self) -> list[DecodeEvent]:
        assert self.mode is not None and self._image is not None
        mode = self.mode
        events: list[DecodeEvent] = []
        while self._state == "imaging":
            frame = self._frame
            pred = self._predict(frame)
            window = self._ms(_SYNC_WINDOW_MS if self._sync_pos else _SYNC_WINDOW_FIRST_MS)
            scale = self._fit[0] / self._nominal_period() if self._fit else 1.0
            need_end = pred + self._ms(mode.max_offset_ms) * scale + window + self._ms(6.0)
            if need_end > self._end:
                break
            measured = self._find_sync(pred, window)
            if measured is not None:
                self._sync_frames.append(frame)
                self._sync_pos.append(measured)
                self._last_sync_frame = frame
                self._refit()
            start = self._line_start(frame, measured)
            scale = self._fit[0] / self._nominal_period() if self._fit else 1.0
            self._decode_frame(frame, start, scale)
            self._frame += 1
            self._image.lines_done = min(mode.height, self._frame * mode.lines_per_frame)

            if self._frame >= mode.frames:
                events.append(self._finish("complete"))
                break
            missed = frame - self._last_sync_frame
            if missed >= _DROPOUT_MIN_LINES and missed * mode.line_ms >= _DROPOUT_S * 1000.0:
                events.append(self._finish("signal lost"))
                break
            if (self._frame - self._last_write_frame) * mode.line_ms >= _WRITE_EVERY_S * 1000.0:
                self._last_write_frame = self._frame
                self._write_png(self._image.lines_done)
                events.append(self._progress_event())
            keep = self._predict(self._frame) + self._ms(mode.min_offset_ms) * scale
            self._trim(keep - self._ms(_SYNC_WINDOW_FIRST_MS) - self._ms(50.0))
        return events

    def _line_start(self, frame: int, measured: float | None) -> float:
        if self._fit is not None:
            fitted = self._predict(frame)
            if measured is None or abs(fitted - measured) <= self._ms(_FIT_DISAGREE_MS):
                return fitted
            return measured
        if measured is not None:
            return measured
        return self._predict(frame)

    def _decode_frame(self, frame: int, start: float, scale: float) -> None:
        assert self.mode is not None and self._image is not None
        mode = self.mode
        offsets = []
        for _channel, off_ms, dur_ms in mode.scans:
            o = self._ms(off_ms) * scale
            d = self._ms(dur_ms) * scale
            offsets.append(start + o + d * np.arange(mode.width + 1) / mode.width)
        freqs = self._slot_freqs(np.stack(offsets))
        values = np.clip(
            (freqs - self._offset_hz - _BLACK_HZ) * (255.0 / (_WHITE_HZ - _BLACK_HZ)), 0, 255
        ).astype(np.float32)

        line = frame * mode.lines_per_frame
        for (channel, _o, _d), vals in zip(mode.scans, values, strict=True):
            if channel == "C":
                self._image.put(self._robot36_chroma_channel(frame, start, scale), line, vals)
            elif channel == "Y0":
                self._image.put("Y", line, vals)
            elif channel == "Y1":
                self._image.put("Y", line + 1, vals)
            else:
                # PD chroma is shared by the frame's two lines.
                for k in range(mode.lines_per_frame):
                    self._image.put(channel, line + k, vals)

    def _robot36_chroma_channel(self, frame: int, start: float, scale: float) -> str:
        """Robot 36 says which chroma a line carries via its separator tone
        (1500 Hz -> R-Y, 2300 Hz -> B-Y); line parity is the fallback."""
        assert self.mode is not None and self.mode.separator is not None
        off_ms, dur_ms = self.mode.separator
        a = start + self._ms(off_ms + 0.5) * scale
        b = start + self._ms(off_ms + dur_ms - 0.5) * scale
        f = self._mean_freq(a, b) - self._offset_hz
        if abs(f - 1500.0) < 250.0:
            return "Cr"
        if abs(f - 2300.0) < 250.0:
            return "Cb"
        return "Cr" if frame % 2 == 0 else "Cb"

    # -- output ----------------------------------------------------------------

    def _slant_ppm(self) -> float | None:
        if self._fit is None:
            return None
        return round((self._fit[0] / self._nominal_period() - 1.0) * 1e6, 1)

    def _write_png(self, lines: int) -> None:
        if self._image is None or self._image_path is None or lines <= 0:
            return
        write_rgb8(self._image_path, self._image.render(lines))

    def _progress_event(self) -> DecodeEvent:
        assert self.mode is not None and self._image is not None
        lines = self._image.lines_done
        return DecodeEvent(
            ts=time.time(),
            decoder="sstv",
            kind="image",
            text=f"SSTV {self.mode.name} · {lines}/{self.mode.height} lines",
            fields={"mode": self.mode.name, "lines": lines, "slant_ppm": self._slant_ppm()},
            update_of_path=str(self._image_path),
            integrity="not_applicable",
            integrity_note=_INTEGRITY_NOTE,
        )

    def _finish(self, reason: str) -> DecodeEvent:
        assert self.mode is not None and self._image is not None
        mode = self.mode
        lines = self._image.lines_done
        if reason == "signal lost":
            # Lines after the last seen sync pulse were decoded from noise.
            lines = min(lines, max(0, (self._last_sync_frame + 1) * mode.lines_per_frame))
        self._write_png(lines)
        complete = lines >= mode.height
        suffix = "" if complete else " (partial)"
        event = DecodeEvent(
            ts=time.time(),
            decoder="sstv",
            kind="image",
            text=f"SSTV {mode.name} · {lines} lines{suffix}",
            fields={
                "mode": mode.name,
                "vis": mode.vis,
                "width": mode.width,
                "height": mode.height,
                "lines": lines,
                "complete": complete,
                "reason": reason,
                "slant_ppm": self._slant_ppm(),
                "tuning_offset_hz": round(self._offset_hz, 1),
            },
            update_of_path=str(self._image_path),
            integrity="not_applicable",
            integrity_note=_INTEGRITY_NOTE,
        )
        self._state = "idle"
        self._vis_scanned_upto = self._end
        self._reset_image()
        return event
