"""HF ALE 2nd generation (MIL-STD-188-141A) call decoder, in-process.

Physical layer: 8-FSK, tones 750-2500 Hz in 250 Hz steps, 125 baud (8 ms
symbols), 3 bits per tone, so the tones are orthogonal over one symbol.

Word layer (this module's reconstruction of the published structure -- the
word fields are right, the bit placement is not checked against an off-air
recording):
 * a word is 24 information bits: a 3-bit preamble then three 7-bit ASCII
   characters (ASCII-64, 0x20-0x5F), MSB first;
 * the 24 bits are split into two 12-bit halves, each extended-Golay(24,12)
   encoded (systematic: 12 data bits then 12 parity bits), giving 48 bits,
   plus one stuffing bit = 49 bits;
 * every word is sent three times at once: tone j of the word's 49 symbols
   carries bit j of copy 1, bit j+16 of copy 2 and bit j+32 of copy 3 (mod 49)
   as its three bits, so one damaged tone costs at most one copy of each of
   three different bits and the bitwise majority of the three copies repairs
   it before Golay runs.
Preambles: DATA 000, THRU 001, TO 010, TWAS 011, FROM 100, TIS 101, CMD 110,
REP 111. An address longer than three characters is carried by consecutive
words with the same preamble ('@' pads the last one).

Decoding: tone decisions come from sliding 64-sample DFTs (hop 8 samples =
1/8 symbol), so no symbol clock is needed -- a word is recognised by the
three de-interleaved copies agreeing on nearly every bit (random tones
agree on only a quarter of them) and then decoded at the best of the
neighbouring sample offsets. Words close together in time form one call; a
call is reported when the words stop (or on flush).

Known limits: no scanning-call/sounding handshake state (each call is
reported independently), no soft decisions beyond the offset search, and
the 188-141A control words beyond the eight preambles are not interpreted
(CMD/DATA/REP text is shown raw).
"""

import time

import numpy as np

from msdr.decoder.backends import DecodeEvent

_FS = 8000
_SYM = 64  # samples per 8 ms symbol
_HOP = 8
_WORD_SYMS = 49
_WORD_HOPS = _WORD_SYMS * _SYM // _HOP  # 392
_SYM_HOPS = _SYM // _HOP  # 8
_TONE0_HZ, _TONE_STEP_HZ = 750.0, 250.0
_COPY_SHIFTS = (0, 16, 32)

_MIN_UNANIMOUS = 42  # of 49 bit positions
# A word read a few symbols early still has its three copies agree (the
# interleave is cyclic, and up to ~3 leading silent symbols fit in the
# tolerance), so unanimity cannot pick the alignment: every candidate within
# this many hops of the first plausible one is Golay-decoded; the fewest
# corrected bits wins, ties go to the purest (best centred) tones.
_CLUSTER_HOPS = 10 * _SYM_HOPS
_MAX_ERRORS = 4
_MIN_TONE_PURITY = 0.5  # mean (peak tone energy / total) over the word
_GAP_WORDS = 2  # silent word-times that end a call

PREAMBLES = ("DATA", "THRU", "TO", "TWAS", "FROM", "TIS", "CMD", "REP")
_PRE_CODE = {"DATA": 0, "THRU": 1, "TO": 2, "TWAS": 3, "FROM": 4, "TIS": 5, "CMD": 6, "REP": 7}
_ADDRESS_PRE = {"TO", "TIS", "TWAS", "FROM", "THRU"}

# -- extended Golay(24,12) ------------------------------------------------------

_GEN = 0xC75  # x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1, generator of the cyclic [23,12] code


def _poly_mod(v: int) -> int:
    for i in range(22, 10, -1):
        if (v >> i) & 1:
            v ^= _GEN << (i - 11)
    return v


def _build_syndromes() -> dict[int, int]:
    """Every 11-bit syndrome <-> its unique error pattern of weight <= 3 (the
    [23,12] Golay code is perfect, so all 2048 are covered)."""
    table = {0: 0}
    for a in range(23):
        table[_poly_mod(1 << a)] = 1 << a
        for b in range(a + 1, 23):
            table[_poly_mod((1 << a) | (1 << b))] = (1 << a) | (1 << b)
            for c in range(b + 1, 23):
                pattern = (1 << a) | (1 << b) | (1 << c)
                table[_poly_mod(pattern)] = pattern
    return table


_SYNDROMES = _build_syndromes()


def golay_encode(data: int) -> int:
    """12 data bits -> 24-bit codeword: data | 11 parity bits | overall parity."""
    c23 = (data << 11) | _poly_mod(data << 11)
    return (c23 << 1) | (bin(c23).count("1") & 1)


def golay_decode(word: int) -> tuple[int, int] | None:
    """24-bit codeword -> (data, corrected bit count), or None when more than
    three bits are wrong (detected, not corrected)."""
    r23, parity = word >> 1, word & 1
    c23 = r23 ^ _SYNDROMES[_poly_mod(r23)]
    errors = bin(r23 ^ c23).count("1")
    if parity != (bin(c23).count("1") & 1):
        errors += 1
    return (c23 >> 11, errors) if errors <= 3 else None


# -- word / symbol coding ---------------------------------------------------------


def _bits(value: int, n: int) -> list[int]:
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def encode_ale_word(preamble: str, chars: str) -> list[int]:
    """Reference encoder: one 49-bit word for `preamble` and exactly three
    ASCII-64 characters."""
    if len(chars) != 3 or any(not 0x20 <= ord(c) <= 0x5F for c in chars):
        raise ValueError("an ALE word carries exactly 3 characters in 0x20-0x5F")
    info = _PRE_CODE[preamble]
    for c in chars:
        info = (info << 7) | ord(c)
    bits = _bits(golay_encode(info >> 12), 24) + _bits(golay_encode(info & 0xFFF), 24)
    return bits + [0]


def word_tones(word: list[int]) -> list[int]:
    """The 49 tone indices (0-7) sending `word` three times, interleaved."""
    return [
        4 * word[j] + 2 * word[(j + _COPY_SHIFTS[1]) % 49] + word[(j + _COPY_SHIFTS[2]) % 49]
        for j in range(_WORD_SYMS)
    ]


def encode_ale_message(parts: list[tuple[str, str]]) -> list[int]:
    """Reference encoder: tone indices for a call given as (preamble, text)
    parts, e.g. [("TO", "X"), ("TIS", "Y")]. Text longer than three characters
    spans several words; the last is padded with '@'."""
    tones: list[int] = []
    for preamble, text in parts:
        text = text.upper()
        for i in range(0, len(text), 3):
            tones.extend(word_tones(encode_ale_word(preamble, text[i : i + 3].ljust(3, "@"))))
    return tones


def tone_hz(index: int) -> float:
    return _TONE0_HZ + _TONE_STEP_HZ * index


# -- decoder ----------------------------------------------------------------------

_DFT = np.exp(
    -2j
    * np.pi
    * np.outer(np.arange(_SYM), [tone_hz(m) for m in range(8)])
    / _FS
)
_J = np.arange(_WORD_SYMS)
_SYM_OFFSETS = _SYM_HOPS * _J  # hop offsets of a word's 49 symbols


def _copies(tones: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """De-interleave tone indices (..., 49) into the three word copies."""
    c1 = (tones >> 2) & 1
    c2 = ((tones >> 1) & 1)[..., (_J - _COPY_SHIFTS[1]) % 49]
    c3 = (tones & 1)[..., (_J - _COPY_SHIFTS[2]) % 49]
    return c1, c2, c3


def _decode_word_bits(bits: np.ndarray) -> tuple[str, str, int] | None:
    halves = []
    errors = 0
    for part in (bits[0:24], bits[24:48]):
        decoded = golay_decode(int("".join(str(int(b)) for b in part), 2))
        if decoded is None:
            return None
        halves.append(decoded[0])
        errors += decoded[1]
    info = (halves[0] << 12) | halves[1]
    chars = [(info >> s) & 0x7F for s in (14, 7, 0)]
    if any(not 0x20 <= c <= 0x5F for c in chars):
        return None
    return PREAMBLES[info >> 21], "".join(chr(c) for c in chars), errors


class AleDecoder:
    """ALE 2G call decoder -- see module docstring."""

    input_rate = _FS

    def __init__(self) -> None:
        self._left = np.zeros(0)
        self._tone = np.zeros(0, dtype=np.int64)  # per hop: strongest tone
        self._purity = np.zeros(0)  # per hop: peak energy / total energy
        self._base = 0  # absolute hop index of _tone[0]
        self._scan_k = 0  # next absolute hop to test as a word start
        self._words: list[tuple[int, str, str, int]] = []  # (hop, preamble, chars, errors)

    def feed(self, samples: np.ndarray) -> list[DecodeEvent]:
        if samples.size == 0:
            return []
        ext = np.concatenate((self._left, samples.astype(np.float64)))
        n_win = (len(ext) - _SYM) // _HOP + 1
        if n_win <= 0:
            self._left = ext
            return []
        windows = np.lib.stride_tricks.sliding_window_view(ext, _SYM)[::_HOP][:n_win]
        energy = np.abs(windows @ _DFT) ** 2
        self._tone = np.concatenate((self._tone, np.argmax(energy, axis=1)))
        total = energy.sum(axis=1)
        purity = np.where(total > 0, energy.max(axis=1) / np.maximum(total, 1e-30), 0.0)
        self._purity = np.concatenate((self._purity, purity))
        self._left = ext[n_win * _HOP :]
        return self._scan()

    def flush(self) -> list[DecodeEvent]:
        events = self._scan(final=True)
        return events + self._emit_call()

    # -- word search ------------------------------------------------------------

    def _scan(self, *, final: bool = False) -> list[DecodeEvent]:
        events: list[DecodeEvent] = []
        end = self._base + len(self._tone)
        last_start = end - 1 - _SYM_HOPS * (_WORD_SYMS - 1)  # latest testable word start
        while self._scan_k <= last_start:
            starts = np.arange(self._scan_k, last_start + 1)
            idx = (starts - self._base)[:, None] + _SYM_OFFSETS
            tones = self._tone[idx]
            c1, c2, c3 = _copies(tones)
            agree = ((c1 == c2) & (c2 == c3)).sum(axis=1)
            purity = self._purity[idx].mean(axis=1)
            ok = np.flatnonzero((agree >= _MIN_UNANIMOUS) & (purity >= _MIN_TONE_PURITY))
            if ok.size == 0:
                self._scan_k = last_start + 1
                break
            first = int(starts[ok[0]])
            if not final and first + _CLUSTER_HOPS > last_start:
                self._scan_k = first  # wait until the neighbouring offsets exist
                break
            best = None
            for i in ok[starts[ok] < first + _CLUSTER_HOPS]:
                word = self._try_word(c1[i], c2[i], c3[i])
                if word is not None and word[2] <= _MAX_ERRORS:
                    key = (word[2], -float(purity[i]))
                    if best is None or key < best[0]:
                        best = (key, int(starts[i]), word)
            if best is None:
                self._scan_k = first + _SYM_HOPS
                continue
            events.extend(self._on_word(best[1], *best[2]))
            # Allow a little timing slack on the next word.
            self._scan_k = best[1] + _WORD_HOPS - 4
        if self._words and end - self._words[-1][0] > _WORD_HOPS * (1 + _GAP_WORDS):
            events.extend(self._emit_call())
        keep = max(min(self._scan_k - self._base, len(self._tone)), 0)
        self._tone, self._purity = self._tone[keep:], self._purity[keep:]
        self._base += keep
        return events

    @staticmethod
    def _try_word(c1, c2, c3) -> tuple[str, str, int] | None:
        # Bitwise majority of the three copies, then Golay.
        return _decode_word_bits(((c1 + c2 + c3) >= 2).astype(np.int64))

    # -- call assembly ------------------------------------------------------------

    def _on_word(self, start: int, preamble: str, chars: str, errors: int) -> list[DecodeEvent]:
        events: list[DecodeEvent] = []
        if self._words and start - self._words[-1][0] > _WORD_HOPS + 2 * _SYM_HOPS:
            events.extend(self._emit_call())  # a gap: the previous call is over
        self._words.append((start, preamble, chars, errors))
        return events

    def _emit_call(self) -> list[DecodeEvent]:
        words, self._words = self._words, []
        if not words:
            return []
        parts: list[list[str]] = []  # [preamble, text]; same-preamble words concatenate
        for _hop, preamble, chars, _err in words:
            if parts and parts[-1][0] == preamble:
                parts[-1][1] += chars
            else:
                parts.append([preamble, chars])
        parts = [[p, t.rstrip("@")] for p, t in parts]
        names = {p for p, _ in parts}
        if "TO" in names:
            call_type = "call"
        elif "TWAS" in names:
            call_type = "sound"
        else:
            call_type = "other"
        text = " ".join(f"{p} {t}" for p, t in parts)
        errors = sum(w[3] for w in words)
        fields = {
            "call_type": call_type,
            "to": " ".join(t for p, t in parts if p == "TO") or None,
            "from": next((t for p, t in parts if p in ("TIS", "TWAS", "FROM")), None),
            "words": len(words),
            "errors": errors,
        }
        return [
            DecodeEvent(
                ts=time.time(),
                decoder="ale",
                kind="text",
                text=text,
                fields=fields,
                # Matrix policy "fec": every word passed Golay with no bit
                # corrected after the 3-copy vote; any correction leaves the
                # call unverified (same convention as NAVTEX).
                integrity="passed" if errors == 0 else "unknown",
                integrity_note=(
                    "Golay(24,12) + 3-copy vote clean"
                    if errors == 0
                    else f"{errors} bit(s) corrected by Golay"
                ),
            )
        ]
