"""RDS (Radio Data System / RBDS) decoder for FM broadcast, pure numpy, streaming.

Chain (all state carried across calls, so it can be fed IQ frame by frame):

  raw IQ (e.g. 8 MS/s) -> `WfmChannel`: complex band-pass decimating FIR
  (the low-pass prototype modulated to the station's offset, so no mixer
  runs at the full input rate) -> 1 MS/s, de-rotate, second FIR -> 250 kHz,
  FM discriminator -> MPX (normalised: 1.0 == 75 kHz deviation).

  MPX -> `RdsDemod`: mix by the nominal 19 kHz pilot and 57 kHz subcarrier,
  decimate both to ~25 kHz through identical filters (so their delays
  match), narrow low-pass (pilot 100 Hz, RDS 2.8 kHz). The measured pilot
  phasor corrects the 57 kHz mix (cubed) *and* is the bit clock: RDS is
  locked to the pilot (1187.5 Bd = 19 kHz / 16), so the symbol clock is the
  unwrapped pilot phase / 16 plus one constant offset, found by a
  max-energy search over 16 candidates of the biphase matched filter
  (integrate first half minus second half of each bit). Coherent BPSK
  decision (carrier phase from the squared symbols), differential decode,
  then block sync on the (26,16) code with the A/B/C/C'/D offset words and
  1-2 bit burst correction once synced.

Stations without a 19 kHz pilot (mono-only RDS, e.g. KNBR 104.5 seen live
2026-09-26) fall back to a free-running 57 kHz mix and the nominal bit clock;
the data-aided carrier phase and timing search absorb the small residual
offset (see RdsDemod.pilot_locked). Synthetic regression:
tests/survey/test_rds.py::test_mono_station_without_pilot_decodes; the
off-air KNBR capture is a separate field-validation case in
tests/corpus/manifest.json.
"""

import itertools

import numpy as np

MPX_TARGET_RATE_HZ = 250_000
PILOT_HZ = 19_000.0
RDS_CARRIER_HZ = 57_000.0
RDS_BITRATE_HZ = 1187.5
WFM_DEVIATION_HZ = 75_000.0
FM_BAND_HZ = (87_500_000, 108_000_000)
FM_CHANNEL_GRID_HZ = 100_000

# (26,16) shortened cyclic code: g(x) = x^10+x^8+x^7+x^5+x^4+x^3+1.
_POLY = 0x5B9
OFFSET_WORDS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "C'": 0x350, "D": 0x1B4}
# Position of each offset word inside a group (C and C' share slot 2).
_BLOCK_SLOT = {"A": 0, "B": 1, "C": 2, "C'": 2, "D": 3}
_SLOT_OFFSETS = [("A",), ("B",), ("C", "C'"), ("D",)]
_OFFSET_BY_SYNDROME = {v: k for k, v in OFFSET_WORDS.items()}
# Lose block sync after this many consecutive uncorrectable blocks (random
# bits pass a 10-bit check ~0.1% of the time, so a lost sync trips this fast).
_SYNC_LOSS_BAD_BLOCKS = 8
# Max distance (in blocks) between two offset-word hits that confirm sync.
_SYNC_MAX_BLOCK_GAP = 6
# While synced, after this many consecutive bad blocks also look for a new
# alignment (a gap in the input slips the bit stream; waiting for the full
# _SYNC_LOSS_BAD_BLOCKS would throw away several more groups).
_RESYNC_AFTER_BAD_BLOCKS = 1
# Symbol timing: a chunk of at least this many bits whose energy at the
# current offset is below this fraction of its best one means a jump.
_TIMING_JUMP_MIN_BITS = 40
_TIMING_JUMP_RATIO = 0.5
# Pilot detection (RdsDemod): per-chunk EMA weight of the mean pilot phasor,
# and the |EMA| hysteresis thresholds to (re)lock / fall back to pilotless.
_PILOT_ACC_ALPHA = 0.1
_PILOT_LOCK = 0.6
_PILOT_UNLOCK = 0.4
_PILOT_WARMUP_CHUNKS = 20
# Soft-decision (Chase) correction: try flipping every 1..3-subset of the
# this-many least reliable symbols of a failed block.
_CHASE_SYMBOLS = 6
_CHASE_MAX_FLIPS = 3

RDS_PTY_NAMES = (
    "None", "News", "Current Affairs", "Information", "Sport", "Education", "Drama",
    "Culture", "Science", "Varied", "Pop Music", "Rock Music", "Easy Listening",
    "Light Classical", "Serious Classical", "Other Music", "Weather", "Finance",
    "Children's Programmes", "Social Affairs", "Religion", "Phone-In", "Travel",
    "Leisure", "Jazz Music", "Country Music", "National Music", "Oldies Music",
    "Folk Music", "Documentary", "Alarm Test", "Alarm",
)  # fmt: skip
RBDS_PTY_NAMES = (
    "None", "News", "Information", "Sports", "Talk", "Rock", "Classic Rock",
    "Adult Hits", "Soft Rock", "Top 40", "Country", "Oldies", "Soft", "Nostalgia",
    "Jazz", "Classical", "Rhythm and Blues", "Soft Rhythm and Blues", "Language",
    "Religious Music", "Religious Talk", "Personality", "Public", "College",
    "Spanish Talk", "Spanish Music", "Hip Hop", "Unassigned", "Unassigned", "Weather",
    "Emergency Test", "Emergency",
)  # fmt: skip
PTY_TABLES = {"rbds": RBDS_PTY_NAMES, "rds": RDS_PTY_NAMES}


def rbds_callsign(pi: int | None) -> str | None:
    """North American (RBDS, NRSC-4) PI code -> the station's call letters,
    which every US station's PI encodes: 0x1000-0x54A7 are K---, 0x54A8-0x994F
    W---. Stations that scroll song text in PS still get a stable name. None
    outside those ranges (Canada/Mexico/Europe PIs, 3-letter call table, TMC
    national codes) -- the PI is then only an ID, not a name."""
    if pi is None:
        return None
    if pi & 0xFF00 == 0xAF00:  # AFxx: regional/national xx00
        pi = (pi & 0xFF) << 8
    elif pi & 0xF000 == 0xA000:  # Axyz: 0x0y0z... compressed form of 0xx0yz
        pi = ((pi & 0x0F00) << 4) | (pi & 0xFF)
    if 0x1000 <= pi <= 0x54A7:
        prefix, value = "K", pi - 0x1000
    elif 0x54A8 <= pi <= 0x994F:
        prefix, value = "W", pi - 0x54A8
    else:
        return None
    letters = (value // 676, (value % 676) // 26, value % 26)
    return prefix + "".join(chr(ord("A") + c) for c in letters)


def in_fm_band(frequency_hz: float) -> bool:
    return FM_BAND_HZ[0] <= frequency_hz <= FM_BAND_HZ[1]


def fm_channel_hz(frequency_hz: float, odd_tenths: bool = False) -> int:
    """Nearest FM channel centre (100 kHz grid): the one key for a station,
    whatever a spectrum label's centroid or side peak says. `odd_tenths`:
    the Americas' 200 kHz grid of odd tenths (88.1, 88.3, ...) instead."""
    if odd_tenths:
        step = 2 * FM_CHANNEL_GRID_HZ
        return int(round((frequency_hz - FM_CHANNEL_GRID_HZ) / step)) * step + FM_CHANNEL_GRID_HZ
    return int(round(frequency_hz / FM_CHANNEL_GRID_HZ)) * FM_CHANNEL_GRID_HZ


# -- filters -----------------------------------------------------------------


def _lowpass(num_taps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    """Hamming-windowed sinc, unity DC gain."""
    n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff_hz / fs * n) * np.hamming(num_taps)
    return h / h.sum()


class _DecimFIR:
    """Streaming decimate-by-D FIR that only computes the kept outputs:
    input reshaped to rows of D samples, output = sum over K tap-rows of
    (rows shifted by k) @ reversed tap-row -- K small matvecs instead of a
    full-rate convolution. Output m depends on input up to index m*D + D-1
    (absolute since construction), which WfmChannel's de-rotation relies on."""

    def __init__(self, taps: np.ndarray, decim: int, dtype=np.complex64) -> None:
        d = int(decim)
        k = -(-len(taps) // d)
        padded = np.zeros(k * d, dtype=np.complex128)
        padded[: len(taps)] = taps
        self._d = d
        self._k = k
        self._dtype = dtype
        self._h = padded.reshape(k, d)[:, ::-1].astype(dtype)
        self._hist = np.zeros((k - 1, d), dtype=dtype)
        self._rem = np.zeros(0, dtype=dtype)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._rem, np.asarray(x, dtype=self._dtype)])
        rows = x.size // self._d
        self._rem = x[rows * self._d :]
        if rows == 0:
            return np.zeros(0, dtype=self._dtype)
        xh = np.concatenate([self._hist, x[: rows * self._d].reshape(rows, self._d)])
        k = self._k
        y = xh[k - 1 : k - 1 + rows] @ self._h[0]
        for j in range(1, k):
            y += xh[k - 1 - j : k - 1 - j + rows] @ self._h[j]
        self._hist = xh[rows:] if k > 1 else self._hist
        return y


class _StreamFIR:
    """Non-decimating streaming FIR (np.convolve over history + block)."""

    def __init__(self, taps: np.ndarray) -> None:
        self._h = np.asarray(taps, dtype=np.complex64)
        self._hist = np.zeros(len(taps) - 1, dtype=np.complex64)

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return np.zeros(0, dtype=np.complex64)
        buf = np.concatenate([self._hist, x.astype(np.complex64)])
        self._hist = buf[buf.size - self._hist.size :]
        return np.convolve(buf, self._h, mode="valid").astype(np.complex64)


# -- WFM channel -> MPX --------------------------------------------------------


class WfmChannel:
    """Selects one WFM station at `offset_hz` from the raw IQ and returns
    its MPX (float32, 1.0 == 75 kHz deviation) at `self.mpx_rate`."""

    def __init__(self, fs_in: float, offset_hz: float) -> None:
        total = max(1, int(round(fs_in / MPX_TARGET_RATE_HZ)))
        # 8 MS/s -> 1 MS/s -> 250 kHz: the first stage has a huge transition
        # band (only aliases of the +-120 kHz station matter), so a short
        # filter does; the second, sharper one runs at 1/8 the rate.
        two_stage = total >= 8 and total % 4 == 0
        d1 = total // 4 if two_stage else total
        r1 = fs_in / d1
        if two_stage:
            h1 = _lowpass(6 * d1, r1 / 2, fs_in)
        else:
            h1 = _lowpass(16 * d1, min(120e3, 0.48 * r1), fs_in)
        w0 = 2 * np.pi * offset_hz / fs_in
        # Band-pass = low-pass prototype modulated to the station: output m
        # comes out multiplied by exp(j*w0*(m*d1 + d1 - 1)), undone below at
        # the decimated rate (see _DecimFIR's indexing).
        g1 = h1 * np.exp(1j * w0 * np.arange(h1.size))
        self._stage1 = _DecimFIR(g1, d1)
        self._derot_step = (w0 * d1) % (2 * np.pi)
        self._derot_phase = (w0 * (d1 - 1)) % (2 * np.pi)
        self._stage2 = _DecimFIR(_lowpass(64, 120e3, r1), 4) if two_stage else None
        self.mpx_rate = fs_in / total
        self._prev = np.complex64(0)
        self._scale = np.float32(self.mpx_rate / (2 * np.pi * WFM_DEVIATION_HZ))

    def process(self, iq: np.ndarray) -> np.ndarray:
        y = self._stage1.process(iq)
        if y.size == 0:
            return np.zeros(0, dtype=np.float32)
        ph = self._derot_phase + self._derot_step * np.arange(y.size)
        y = y * np.exp(-1j * ph).astype(np.complex64)
        self._derot_phase = float((self._derot_phase + self._derot_step * y.size) % (2 * np.pi))
        if self._stage2 is not None:
            y = self._stage2.process(y)
            if y.size == 0:
                return np.zeros(0, dtype=np.float32)
        z = np.concatenate([[self._prev], y])
        self._prev = z[-1]
        mpx = np.angle(z[1:] * np.conj(z[:-1])).astype(np.float32)
        return mpx * self._scale


# -- block code ------------------------------------------------------------------


def syndrome(word26: int) -> int:
    """Remainder of the 26-bit block mod g(x). For a valid block this is
    exactly its offset word (checkword = m*x^10 mod g XOR offset)."""
    reg = word26
    for i in range(25, 9, -1):
        if (reg >> i) & 1:
            reg ^= _POLY << (i - 10)
    return reg


def _burst_table(max_burst: int) -> dict[int, int]:
    """syndrome -> error pattern for every burst of length 1..max_burst
    (burst = first and last bit set) inside a 26-bit block."""
    table: dict[int, int] = {}
    for length in range(1, max_burst + 1):
        inner = [0] if length <= 2 else range(1 << (length - 2))
        for mid in inner:
            pattern = 1 if length == 1 else (1 | (mid << 1) | (1 << (length - 1)))
            for shift in range(0, 27 - length):
                e = pattern << shift
                table.setdefault(syndrome(e), e)
    return table


def _symbol_error(q: int) -> int:
    """26-bit error pattern of one wrong symbol, for the 27 symbols behind a
    block (q=0 precedes bit 0, q=k+1 is bit k's own): a differential bit
    is the XOR of two consecutive symbols, so one symbol hits two bits."""
    err = 0
    for bit in (q - 1, q):
        if 0 <= bit <= 25:
            err |= 1 << (25 - bit)
    return err


_SYMBOL_ERRORS = [_symbol_error(q) for q in range(27)]
_SYMBOL_SYNDROMES = [syndrome(e) for e in _SYMBOL_ERRORS]
_CHASE_COMBOS = [
    c
    for n in range(1, _CHASE_MAX_FLIPS + 1)
    for c in itertools.combinations(range(_CHASE_SYMBOLS), n)
]


def block_word(data16: int, offset: str) -> int:
    return (data16 << 10) | (syndrome(data16 << 10) ^ OFFSET_WORDS[offset])


# -- demodulator -----------------------------------------------------------------


class RdsGroup:
    __slots__ = ("blocks", "version_b_by_offset")

    def __init__(self, blocks: list[int | None], version_b_by_offset: bool) -> None:
        self.blocks = blocks  # [A, B, C, D] 16-bit data or None if bad
        self.version_b_by_offset = version_b_by_offset


class RdsDemod:
    """MPX -> RDS groups. `process(mpx)` returns completed groups; block
    counters (in-sync blocks / uncorrectable ones) via `pop_block_stats`."""

    _N_TIMING = 16

    def __init__(self, mpx_rate: float, max_burst: int = 2) -> None:
        fs = float(mpx_rate)
        d = max(1, int(round(fs / 25_000)))
        self.rate = fs / d
        self._w19 = 2 * np.pi * PILOT_HZ / fs
        self._ph19 = 0.0
        dec = _lowpass(12 * d, min(8_000.0, 0.4 * self.rate), fs)
        self._pilot_dec = _DecimFIR(dec, d)
        self._rds_dec = _DecimFIR(dec, d)
        # Same length for both narrow filters -> same group delay, so the
        # pilot phasor lines up sample-for-sample with the RDS baseband.
        self._pilot_fir = _StreamFIR(_lowpass(241, 100.0, self.rate))
        self._rds_fir = _StreamFIR(_lowpass(241, 2_800.0, self.rate))
        self._m = 0  # absolute output-sample index at self.rate
        self._theta = 0.0  # last unwrapped pilot phase error
        # symbol timing
        self._energy = np.zeros(self._N_TIMING)
        self._tau_idx: int | None = None
        self._rem_r = np.zeros(0, dtype=np.complex64)
        self._rem_psi = np.zeros(0, dtype=np.float64)
        self._next_bit: int | None = None
        self._carrier_acc = 0j
        self._carrier_phase = 0.0
        self._last_d = 0
        # block sync
        self._corrections = _burst_table(max_burst) if max_burst > 0 else {}
        self._reg = 0
        # |soft value| of the 27 symbols behind the register's 26 bits (a
        # differential bit depends on its own symbol and the one before).
        self._rel = [np.inf] * 27
        self._bit_count = 0
        self._last_hit: tuple[int, int] | None = None  # (bit position, slot)
        self._synced = False
        self._slot = 0
        self._bits_in_block = 0
        self._bad_run = 0
        self._group: list[int | None] = [None] * 4
        self._group_b = False
        self._blocks = 0
        self._block_errors = 0
        self.pilot_level = 0.0
        # Pilot presence: EMA of the chunk-mean pilot phasor. A real pilot
        # holds its phase (|.| -> 1); a mono station's 19 kHz bin is noise
        # whose phase wanders (|.| -> ~0) -- a level threshold couldn't tell
        # them apart since the noise level depends on the station's deviation.
        self._pilot_acc = 0j
        self._pilot_chunks = 0
        self.pilot_locked = True

    # -- DSP ----------------------------------------------------------------

    def process(self, mpx: np.ndarray) -> list[RdsGroup]:
        n = mpx.size
        if n == 0:
            return []
        ph = self._ph19 + self._w19 * np.arange(n)
        self._ph19 = float((self._ph19 + self._w19 * n) % (2 * np.pi))
        lo19 = np.exp(-1j * ph).astype(np.complex64)
        lo57 = lo19 * lo19 * lo19
        x = mpx.astype(np.float32)
        p = self._pilot_fir.process(self._pilot_dec.process(x * lo19))
        r = self._rds_fir.process(self._rds_dec.process(x * lo57))
        if p.size == 0:
            return []
        mag = np.abs(p)
        self.pilot_level = float(np.mean(mag)) * 2.0
        u = p / np.maximum(mag, 1e-12)
        self._pilot_acc = (1 - _PILOT_ACC_ALPHA) * self._pilot_acc + _PILOT_ACC_ALPHA * complex(
            np.mean(u)
        )
        self._pilot_chunks += 1
        # Until the EMA has warmed up (it starts at 0) keep the pilot mode:
        # deciding early flipped every stereo station to pilotless and back.
        locked = self._pilot_chunks < _PILOT_WARMUP_CHUNKS or abs(self._pilot_acc) >= (
            _PILOT_UNLOCK if self.pilot_locked else _PILOT_LOCK
        )
        if locked != self.pilot_locked:
            # The two modes' bit-clock phases differ: re-acquire timing and
            # carrier from scratch rather than slip mid-block.
            self.pilot_locked = locked
            self._reset_timing()
        theta = np.unwrap(np.concatenate([[self._theta], np.angle(u).astype(np.float64)]))[1:]
        self._theta = float(theta[-1])
        m = self._m + np.arange(p.size, dtype=np.float64)
        self._m += p.size
        if not locked:
            # Mono station (no pilot): the free-running 57 kHz mix and the
            # nominal bit clock -- the subcarrier is crystal-accurate (live:
            # KNBR 104.5 at 57 000.08 Hz), and _symbols' data-aided carrier
            # phase and timing search absorb the small residual offset.
            return self._symbols(r, m * (RDS_BITRATE_HZ / self.rate))
        # Pilot's own phase error cubed corrects the 57 kHz mix.
        rb = r * np.conj(u * u * u)
        # Bit clock in cycles: pilot phase / 16 (constant offset found below).
        psi = m * (RDS_BITRATE_HZ / self.rate) + theta / (32 * np.pi)
        return self._symbols(rb, psi)

    def _reset_timing(self) -> None:
        self._energy = np.zeros(self._N_TIMING)
        self._tau_idx = None
        self._rem_r = np.zeros(0, dtype=np.complex64)
        self._rem_psi = np.zeros(0, dtype=np.float64)
        self._next_bit = None
        self._carrier_acc = 0j

    def _soft_bits(self, r: np.ndarray, psi: np.ndarray, tau: float):
        pos = psi - tau
        k = np.floor(pos).astype(np.int64)
        w = np.where(pos - k < 0.5, 1.0, -1.0)
        k0 = int(k[0])
        idx = k - k0
        re = np.bincount(idx, weights=w * r.real)
        im = np.bincount(idx, weights=w * r.imag)
        return k0, re + 1j * im, k

    def _symbols(self, rb: np.ndarray, psi: np.ndarray) -> list[RdsGroup]:
        r = np.concatenate([self._rem_r, rb])
        p = np.concatenate([self._rem_psi, psi])
        if p[-1] - p[0] < 2.0:
            self._rem_r, self._rem_psi = r, p
            return []
        energies = np.empty(self._N_TIMING)
        for i in range(self._N_TIMING):
            _k0, soft, _k = self._soft_bits(r, p, i / self._N_TIMING)
            energies[i] = float(np.sum(np.abs(soft[1:-1]) ** 2))
        n_bits = p[-1] - p[0]
        if (
            self._tau_idx is not None
            and n_bits >= _TIMING_JUMP_MIN_BITS
            and energies[self._tau_idx] < _TIMING_JUMP_RATIO * energies.max()
        ):
            # The current offset has clearly lost its energy: the input had
            # a gap (dropped IQ frames), not drift. Re-acquire from this
            # chunk alone instead of waiting for the average to catch up.
            self._energy = energies.copy()
            self._tau_idx = int(np.argmax(energies))
        else:
            self._energy = 0.7 * self._energy + 0.3 * energies
        best = int(np.argmax(self._energy))
        # Hysteresis: the clock is pilot-locked, so the right offset never
        # moves -- switching only on a clear winner avoids bit slips.
        if self._tau_idx is None or self._energy[best] > 1.2 * self._energy[self._tau_idx]:
            self._tau_idx = best
        k0, soft, k = self._soft_bits(r, p, self._tau_idx / self._N_TIMING)
        k_last = int(k[-1])  # still incomplete
        first = k0 + 1 if self._next_bit is None else max(self._next_bit, k0)
        keep = k >= k_last
        self._rem_r, self._rem_psi = r[keep], p[keep]
        self._next_bit = k_last
        if first >= k_last:
            return []
        s = soft[first - k0 : k_last - k0]
        acc = complex(np.sum(s * s))
        if self._carrier_acc == 0 or np.cos(np.angle(acc) - np.angle(self._carrier_acc)) < 0:
            # (Re)start, or a carrier phase jump of > 45 deg (a gap in the
            # input, or a 57 kHz subcarrier not locked to the pilot): the
            # averaged estimate would be wrong for several chunks.
            self._carrier_acc = acc
        else:
            self._carrier_acc = 0.7 * self._carrier_acc + 0.3 * acc
        phase = float(np.angle(self._carrier_acc)) / 2
        # angle/2 is ambiguous by pi; stay on the branch nearest the last
        # estimate so a wrap doesn't flip every bit's polarity mid-stream.
        if np.cos(phase - self._carrier_phase) < 0:
            phase += np.pi
        self._carrier_phase = phase
        soft = np.real(s * np.exp(-1j * phase))
        d = (soft > 0).astype(np.int8)
        prev = np.concatenate([[self._last_d], d[:-1]]).astype(np.int8)
        self._last_d = int(d[-1])
        return self.feed_bits((d ^ prev).tolist(), np.abs(soft).tolist())

    # -- block sync / groups -------------------------------------------------

    def pop_block_stats(self) -> tuple[int, int]:
        out = (self._blocks, self._block_errors)
        self._blocks = self._block_errors = 0
        return out

    def feed_bits(self, bits, reliability=None) -> list[RdsGroup]:
        """Differentially-decoded message bits -> completed groups.
        `reliability` (optional, same length): |soft value| of the symbol
        each bit was decoded with; enables soft-decision correction."""
        groups: list[RdsGroup] = []
        rel = self._rel
        for i, bit in enumerate(bits):
            self._reg = ((self._reg << 1) | int(bit)) & 0x3FFFFFF
            del rel[0]
            rel.append(reliability[i] if reliability is not None else np.inf)
            self._bit_count += 1
            if not self._synced:
                self._try_sync()
                continue
            # Off the current block boundaries only: a hit on them is the
            # current alignment itself, handled by _take_block below.
            if (
                self._bad_run >= _RESYNC_AFTER_BAD_BLOCKS
                and self._bits_in_block != 25
                and self._try_sync()
            ):
                continue
            self._bits_in_block += 1
            if self._bits_in_block < 26:
                continue
            self._bits_in_block = 0
            group = self._take_block()
            if group is not None:
                groups.append(group)
        return groups

    def _try_sync(self) -> bool:
        """Offset-word search; True when two hits confirmed a (new) sync."""
        name = _OFFSET_BY_SYNDROME.get(syndrome(self._reg))
        if name is None:
            return False
        slot = _BLOCK_SLOT[name]
        hit = (self._bit_count, slot)
        if self._last_hit is not None:
            dist = self._bit_count - self._last_hit[0]
            gap = dist // 26
            if (
                dist % 26 == 0
                and 0 < gap <= _SYNC_MAX_BLOCK_GAP
                and ((self._last_hit[1] + gap) % 4 == slot)
            ):
                self._synced = True
                self._bad_run = 0
                self._bits_in_block = 0
                self._group = [None] * 4
                self._group_b = name == "C'"
                self._store(slot, self._reg >> 10, name)
                self._slot = (slot + 1) % 4
                self._last_hit = None
                return True
        self._last_hit = hit
        return False

    def _chase(self, word: int, synd: int, slot: int) -> tuple[int | None, str | None]:
        """Soft-decision correction: flip the least reliable symbols (each
        flips two adjacent differential bits), cheapest combination first."""
        rel = self._rel
        if not np.isfinite(rel).all():
            return None, None
        weak = sorted(range(27), key=rel.__getitem__)[:_CHASE_SYMBOLS]
        wanted = {OFFSET_WORDS[c]: c for c in _SLOT_OFFSETS[slot]}
        best: tuple[float, int, str] | None = None
        for combo in _CHASE_COMBOS:
            cost = 0.0
            e_synd = 0
            err = 0
            for j in combo:
                q = weak[j]
                cost += rel[q]
                e_synd ^= _SYMBOL_SYNDROMES[q]
                err ^= _SYMBOL_ERRORS[q]
            name = wanted.get(synd ^ e_synd)
            if name is not None and (best is None or cost < best[0]):
                best = (cost, err, name)
        if best is None:
            return None, None
        return (word ^ best[1]) >> 10, best[2]

    def _store(self, slot: int, data: int | None, name: str | None) -> None:
        if slot == 0:
            self._group = [None] * 4
            self._group_b = False
        self._group[slot] = data
        if name == "C'":
            self._group_b = True

    def _take_block(self) -> RdsGroup | None:
        slot = self._slot
        word = self._reg
        synd = syndrome(word)
        data = None
        name = None
        for cand in _SLOT_OFFSETS[slot]:
            if synd == OFFSET_WORDS[cand]:
                data, name = word >> 10, cand
                break
        if data is None:
            data, name = self._chase(word, synd, slot)
        if data is None:
            for cand in _SLOT_OFFSETS[slot]:
                err = self._corrections.get(synd ^ OFFSET_WORDS[cand])
                if err is not None:
                    data, name = (word ^ err) >> 10, cand
                    break
        self._blocks += 1
        if data is None:
            self._block_errors += 1
            self._bad_run += 1
            if self._bad_run >= _SYNC_LOSS_BAD_BLOCKS:
                self._synced = False
                self._last_hit = None
        else:
            self._bad_run = 0
        self._store(slot, data, name)
        self._slot = (slot + 1) % 4
        if slot == 3:
            return RdsGroup(list(self._group), self._group_b)
        return None


# -- group parser / per-station accumulator ------------------------------------------


def _chars(word: int) -> str:
    out = []
    for byte in ((word >> 8) & 0xFF, word & 0xFF):
        out.append(chr(byte) if 0x20 <= byte <= 0x7E or byte == 0x0D else "?")
    return "".join(out)


class RdsParser:
    """Accumulates groups for one station, possibly across several dwells.
    PI, PTY, PS segments and RT segments are only accepted once the same
    value has been seen twice in a row -- a miscorrected block can't put a
    wrong character into the PS on its own."""

    def __init__(self, pty_table: str = "rbds") -> None:
        self._pty_names = PTY_TABLES[pty_table]
        self._pi_cand: int | None = None
        self.pi: int | None = None
        self._pty_cand: tuple[int, bool] | None = None
        self.pty: int | None = None
        self.tp: bool | None = None
        self._ps_cand: list[str | None] = [None] * 4
        self._ps: list[str | None] = [None] * 4
        self._ps_done: str | None = None  # last complete, consistent PS
        self._rt_ab: int | None = None
        self._rt_b: bool | None = None
        self._rt_cand: list[str | None] = [None] * 16
        self._rt: list[str | None] = [None] * 16
        self.groups = 0
        self.blocks = 0
        self.block_errors = 0

    def add_block_stats(self, blocks: int, errors: int) -> None:
        self.blocks += blocks
        self.block_errors += errors

    def _accept_pi(self, pi: int) -> None:
        if self._pi_cand == pi:
            self.pi = pi
        self._pi_cand = pi

    def feed(self, group: RdsGroup) -> None:
        a, b, c, d = group.blocks
        if a is not None:
            self._accept_pi(a)
        if b is None:
            return
        self.groups += 1
        gtype = (b >> 12) & 0xF
        version_b = bool((b >> 11) & 1)
        if version_b and c is not None and group.version_b_by_offset:
            self._accept_pi(c)
        pty_tp = ((b >> 5) & 0x1F, bool((b >> 10) & 1))
        if self._pty_cand == pty_tp:
            self.pty, self.tp = pty_tp
        self._pty_cand = pty_tp
        if gtype == 0 and d is not None:
            addr = b & 0x3
            chars = _chars(d)
            if self._ps_cand[addr] == chars:
                if self._ps[addr] is not None and self._ps[addr] != chars:
                    # A confirmed segment changed: dynamic ("scrolling") PS
                    # moved to a new page. The other segments must be
                    # re-confirmed, or the PS would mix two pages.
                    self._ps = [None] * 4
                self._ps[addr] = chars
                if all(seg is not None for seg in self._ps):
                    self._ps_done = "".join(self._ps).strip() or None  # type: ignore[arg-type]
            self._ps_cand[addr] = chars
        elif gtype == 2:
            self._feed_rt(b, c, d, version_b)

    def _feed_rt(self, b: int, c: int | None, d: int | None, version_b: bool) -> None:
        ab = (b >> 4) & 1
        if ab != self._rt_ab or version_b != self._rt_b:
            # A/B flag toggle = "new text, clear the display".
            self._rt_ab, self._rt_b = ab, version_b
            self._rt_cand = [None] * 16
            self._rt = [None] * 16
        addr = b & 0xF
        if version_b:
            if d is None:
                return
            chars = _chars(d)
        else:
            if c is None or d is None:
                return
            chars = _chars(c) + _chars(d)
        if self._rt_cand[addr] == chars:
            self._rt[addr] = chars
        self._rt_cand[addr] = chars

    @property
    def ps(self) -> str | None:
        return self._ps_done

    @property
    def rt(self) -> str | None:
        text = ""
        for seg in self._rt:
            if seg is None:
                return None
            text += seg
            if "\r" in seg:
                break
        text = text.split("\r", 1)[0].strip()
        return text or None

    def result(self) -> dict:
        return {
            "pi": f"0x{self.pi:04X}" if self.pi is not None else None,
            "callsign": rbds_callsign(self.pi) if self._pty_names is RBDS_PTY_NAMES else None,
            "ps": self.ps,
            "pty": self.pty,
            "pty_name": self._pty_names[self.pty] if self.pty is not None else None,
            "rt": self.rt,
            "tp": self.tp,
            "groups": self.groups,
            "block_error_rate": (self.block_errors / self.blocks) if self.blocks else 0.0,
        }


class RdsReceiver:
    """WfmChannel + RdsDemod feeding an RdsParser (which may outlive this
    receiver -- a new receiver per dwell, one parser per station)."""

    def __init__(self, fs_in: float, offset_hz: float, parser: RdsParser | None = None) -> None:
        self.channel = WfmChannel(fs_in, offset_hz)
        self.demod = RdsDemod(self.channel.mpx_rate)
        self.parser = parser if parser is not None else RdsParser()

    def process(self, iq: np.ndarray) -> None:
        for group in self.demod.process(self.channel.process(iq)):
            self.parser.feed(group)
        self.parser.add_block_stats(*self.demod.pop_block_stats())


# -- synthetic encoder (tests / self-check) --------------------------------------------


def _text_words(text: str, length: int) -> list[int]:
    raw = text.encode("ascii", "replace")[:length]
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]


def encode_groups(ps: str, pi: int, rt: str | None, n_groups: int, pty: int = 10) -> list:
    """Group sequence: 0A PS segments interleaved with 2A RadioText ones.
    Returns [(A, B, C, D, c_offset_name), ...]."""
    ps_words = _text_words(ps.ljust(8)[:8], 8)
    rt_words: list[int] = []
    if rt:
        body = rt[:64]
        if len(body) < 64:
            body += "\r"
        body = body.ljust(-(-len(body) // 4) * 4)
        rt_words = _text_words(body, 64)
    base_b = (pty & 0x1F) << 5
    out = []
    ps_seg = rt_seg = 0
    for i in range(n_groups):
        if rt_words and i % 2 == 1:
            n_seg = len(rt_words) // 2
            b = (2 << 12) | base_b | rt_seg
            out.append((pi, b, rt_words[2 * rt_seg], rt_words[2 * rt_seg + 1], "C"))
            rt_seg = (rt_seg + 1) % n_seg
        else:
            b = base_b | ps_seg
            out.append((pi, b, 0xE0CD, ps_words[ps_seg], "C"))
            ps_seg = (ps_seg + 1) % 4
    return out


def encode_rds_bits(ps: str, pi: int, rt: str | None, n_groups: int, pty: int = 10) -> np.ndarray:
    bits = []
    for a, b, c, d, c_name in encode_groups(ps, pi, rt, n_groups, pty):
        for data, name in ((a, "A"), (b, "B"), (c, c_name), (d, "D")):
            word = block_word(data, name)
            bits.extend((word >> (25 - i)) & 1 for i in range(26))
    return np.array(bits, dtype=np.int8)


def encode_rds_mpx(
    ps: str,
    pi: int,
    rt: str | None,
    seconds: float,
    fs: float,
    *,
    pty: int = 10,
    rds_level: float = 0.04,
    pilot_level: float = 0.09,
    audio: bool = True,
    t0: float = 0.0123,
) -> np.ndarray:
    """Stereo-like MPX (1.0 == full deviation): pilot, optional L+R / L-R
    tones, and a 57 kHz DSB-SC differential biphase RDS signal whose bit
    clock is locked to the pilot (as the standard requires)."""
    n = int(round(seconds * fs))
    t = t0 + np.arange(n) / fs
    bit_pos = t * RDS_BITRATE_HZ
    k = np.floor(bit_pos).astype(np.int64)
    n_bits = int(k[-1] - k[0]) + 1
    bits = encode_rds_bits(ps, pi, rt, n_bits // 104 + 2, pty)[:n_bits]
    diff = np.bitwise_xor.accumulate(bits)
    sym = 2.0 * diff[k - k[0]] - 1.0
    sym *= np.where(bit_pos - k < 0.5, 1.0, -1.0)
    shaped = np.convolve(sym, _lowpass(401, 2_400.0, fs), mode="same")
    shaped /= np.max(np.abs(shaped)) + 1e-12
    pilot_phase = 2 * np.pi * PILOT_HZ * t
    mpx = pilot_level * np.sin(pilot_phase) + rds_level * shaped * np.cos(3 * pilot_phase)
    if audio:
        mpx += 0.35 * np.sin(2 * np.pi * 1_000.0 * t)
        mpx += 0.2 * np.sin(2 * np.pi * 2_500.0 * t) * np.sin(2 * pilot_phase)
    return mpx


def fm_modulate_chunks(
    mpx: np.ndarray,
    fs_mpx: float,
    fs_out: float,
    offset_hz: float,
    chunk: int,
    *,
    amplitude: float = 0.5,
    deviation_hz: float = WFM_DEVIATION_HZ,
):
    """Yield complex64 IQ chunks at `fs_out` carrying `mpx` FM-modulated
    at `offset_hz` (MPX linearly interpolated up to fs_out)."""
    total = int(mpx.size * fs_out / fs_mpx)
    src = np.arange(mpx.size, dtype=np.float64)
    phase = 0.0
    for start in range(0, total, chunk):
        idx = np.arange(start, min(start + chunk, total), dtype=np.float64)
        m = np.interp(idx * (fs_mpx / fs_out), src, mpx)
        inc = 2 * np.pi * (offset_hz + deviation_hz * m) / fs_out
        ph = phase + np.cumsum(inc)
        phase = float(ph[-1] % (2 * np.pi))
        yield (amplitude * np.exp(1j * ph)).astype(np.complex64)
