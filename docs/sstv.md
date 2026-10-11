# SSTV decoder

In-process slow-scan television decoder for HF/VHF amateur SSTV pictures, for operators and for anyone extending the mode table.

- Code: `msdr/decoder/pydecoders/sstv.py` (PythonBackend); registry id `sstv`, kind `image`.
- In: 11,025 Hz mono audio. Out: one RGB PNG per picture, written progressively under the decoder's fax/image directory exactly like the WEFAX decoder (start event carries `image_path`; progress and finish events carry `update_of_path`).
- Timings follow JL Barber N7CXI, *Proposal for SSTV Mode Specifications* (Dayton SSTV forum, 20 May 2000), the de-facto reference every current SSTV program (MMSSTV, QSSTV, slowrx, Robot36 for Android) encodes to.

## Supported modes

Sync pulses are 1200 Hz; video is 1500 Hz (black) to 2300 Hz (white).
Line time is sync-to-sync.

| Mode | VIS | Size | Line (ms) | Sync (ms) | Scan per channel (ms) | Colour / order |
|---|---|---|---|---|---|---|
| Robot 36 | 8 | 320x240 | 150.0 | 9.0 | Y 88, chroma 44 | YCrCb 4:2:0 -- R-Y on even lines (1500 Hz separator), B-Y on odd lines (2300 Hz separator) |
| Robot 72 | 12 | 320x240 | 300.0 | 9.0 | Y 138, R-Y 69, B-Y 69 | YCrCb 4:2:2 |
| Martin M1 | 44 | 320x256 | 446.446 | 4.862 | 146.432 | G, B, R |
| Martin M2 | 40 | 320x256 | 226.798 | 4.862 | 73.216 | G, B, R |
| Scottie S1 | 60 | 320x256 | 428.22 | 9.0 | 138.240 | G, B, *sync*, R |
| Scottie S2 | 56 | 320x256 | 277.692 | 9.0 | 88.064 | G, B, *sync*, R |
| Scottie DX | 76 | 320x256 | 1050.3 | 9.0 | 345.600 | G, B, *sync*, R |
| PD 50 | 93 | 320x256 | 388.16 | 20.0 | 91.520 | Y(even), R-Y, B-Y, Y(odd) -- two lines per sync |
| PD 90 | 99 | 320x256 | 703.04 | 20.0 | 170.240 | same |
| PD 120 | 95 | 640x496 | 508.48 | 20.0 | 121.600 | same |
| PD 180 | 96 | 640x496 | 754.24 | 20.0 | 183.040 | same |
| Wraase SC2-180 | 55 | 320x256 | 711.0225 | 5.5225 | 235.0 | R, G, B |

### Porches and separators

| Family | Porch / separator |
|---|---|
| Robot | 3 ms 1500 Hz after sync; 4.5 ms separator + 1.5 ms 1900 Hz porch before each chroma scan |
| Martin | 0.572 ms 1500 Hz after sync and after every scan |
| Scottie | 1.5 ms 1500 Hz before each scan |
| PD | 2.08 ms 1500 Hz after sync |
| Wraase | 0.5 ms after sync |

### Verification

- Round trip against an independently written encoder (`tests/decoder/sstv_encoder.py`, timings typed in again from the paper): Robot 36, Robot 72, Martin M1, Scottie S1, PD 120.
- Martin M1 is additionally verified against a third-party QSSTV recording (`tests/decoder/data/`).
- Martin M2, Scottie S2/DX, PD 50/90/180 and SC2-180 share the same code paths and only differ in table entries, but have no test fixture.

### Colour

- Robot and PD use the paper's BT.601 studio-range equations (Y = 16 + 0.003906 (65.738 R + 129.057 G + 25.064 B), etc.); the decoder applies the matching inverse.
- Robot 36 chroma is shared across each line pair; the separator tone (1500 / 2300 Hz) decides whether a line carries R-Y or B-Y, with line parity as the fallback if the separator is unreadable.

## How detection works

1. Front end: a 129-tap complex band-pass FIR (950-2550 Hz) yields the
   analytic signal; its per-sample phase rotation is the instantaneous
   frequency, and its running sum an unwrapped phase.
2. Header: a run of >= 150 ms within +-120 Hz of 1900 Hz that ends in a
   drop to 1200 Hz is a VIS candidate (the nominal leader is 300 ms; the
   first leader, before the 10 ms break, is also tried and simply fails).
   The leader's mean frequency gives the receiver's tuning offset (accepted
   up to +-150 Hz) and is subtracted from every tone afterwards, so a USB
   dial a few tens of Hz off still decodes with correct brightness.
3. VIS: each of the ten 30 ms bits (start, 7 data + even parity, stop) is
   classified over its middle 20 ms with a 3-bin DFT at 1100 / 1200 /
   1300 Hz (Goertzel-equivalent, 20 ms makes the three bins orthogonal).
   Start and stop must be 1200 Hz, no data bit may be 1200 Hz, parity must
   be even, and the code must be in the table; only then is a "SSTV <mode>
   started" image event emitted. There is no sync-timing fallback when the
   VIS is missed (see limitations).
4. Lines: each line's sync pulse is searched in a window (+-20 ms for the
   first, +-4 ms afterwards) around its predicted position by thresholding
   the box-smoothed frequency at 1350 Hz and taking the pulse's trailing
   edge (1200 -> 1500 Hz porch) as the reference -- the leading edge is
   unusable on the first line, where the stop bit runs straight into the
   sync. An amplitude gate (10 % of the leader's level) rejects "syncs"
   found in silence. Found positions feed a least-squares line fit (last 64
   syncs); its slope is the measured line period, so a sample-clock error
   on either side shows up as `slant_ppm` and is corrected, and each line
   starts where the fit says (or at the measured sync when the two disagree
   by more than 1.5 ms). Scottie's mid-line sync is handled by giving its
   G and B scans negative offsets from the sync.
5. Pixels: a pixel's value is the mean frequency over its time slot, taken
   as (phase difference across the slot) / (slot length) on the unwrapped
   phase. The slot edges are computed from the fitted line start and
   period, so any measured line length resamples to the nominal width.
6. Output: the PNG is rewritten (atomically) about every 1.5 s of picture
   and a progress event updates the stored start event. The picture
   finishes when all lines are in, when no sync pulse has been found for
   >= 2 s and >= 3 lines ("signal lost" -- rows after the last seen sync
   are dropped, the rest is kept as a partial image), or when the decoder is
   stopped. The decoder then goes back to idle and will catch the next
   header.

Throughput: a 38 s Robot 36 transmission decodes in ~0.15 s, a 128 s PD 120
in ~1.6 s (M-series laptop, numpy only) -- roughly 100-250x real time.

## Tuning

| Band | Mode | Notes |
|---|---|---|
| HF | USB | Set the dial to the listed frequency (SSTV frequencies are quoted as the suppressed-carrier/dial frequency, unlike radiofax); video then sits at 1200-2300 Hz audio. Common: 14.230 MHz (also 14.233), 7.171 MHz, 3.730 MHz, 21.340 MHz, 28.680 MHz |
| VHF | NFM | 144.500 MHz (Europe) and ISS/ARISS events on 145.800 MHz (usually PD 120, sometimes Robot 36 or PD 180). Keep the receiver's squelch open or low — a squelch closing between lines looks like a dropout |

Bandwidth: the registry default is 3 kHz. A tuning error up to about +-150 Hz is measured from the header and compensated; beyond that the header is not recognised.

## Limitations

* No FSK-ID / CW-ID decoding (the station ID that follows some pictures).
* No VIS-less detection: a picture whose header was missed (tuned in late,
  deep fade during the VIS) is not decoded. A sync-timing fallback was
  planned but is not implemented.
* No 16-bit extended VIS (MP/ML/MR modes, PD 240/290 etc. are absent).
* Slant correction tracks sync pulses only; there is no manual slant
  adjustment, no auto-slant from image content, and no re-synchronisation
  if the fit is lost for longer than the dropout timeout.
* The input rate is 11,025 Hz, so a Robot 36 luma pixel spans ~3 samples
  and a chroma pixel ~1.5, a PD 120 pixel ~2.1: the chroma channels are
  the first to get noisy on weak signals (the test suite's 15 dB SNR case
  gives a mean absolute error of ~13/255 on Robot 36 vs ~6/255 on Martin
  M1), and single-sample phase slips show as isolated coloured speckles.
* Robot 36 chroma lines are identified by their separator tone; a line
  whose separator is unreadable falls back to parity, which is wrong if a
  line was lost earlier. Robot 72 / PD chroma is assumed per the table and
  not verified against the separator.
* Images are RGB PNGs only; no de-interlace/line-doubling, sharpening or
  colour correction is applied.

See also: [decoder-expansion.md](decoder-expansion.md), [receiver-dsp.md](receiver-dsp.md), [validation/](validation/).
