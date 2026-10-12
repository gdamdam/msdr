# SSTV decoder

msdr decodes slow-scan television (SSTV) pictures from HF and VHF amateur transmissions, in-process. This page covers tuning, the supported modes and the decoder's limits, for operators and for anyone extending the mode table.

- Code: `msdr/decoder/pydecoders/sstv.py` (PythonBackend). Registry id `sstv`, kind `image`.
- Input: 11,025 Hz mono audio.
- Output: one RGB PNG per picture, written progressively under the decoder's fax/image directory, the same way as the WEFAX decoder. The start event carries `image_path`; progress and finish events carry `update_of_path`.
- Timings follow JL Barber N7CXI, *Proposal for SSTV Mode Specifications* (Dayton SSTV forum, 20 May 2000). Current SSTV programs (MMSSTV, QSSTV, slowrx, Robot36 for Android) encode to this reference.

## Tuning

| Band | Mode | Notes |
|---|---|---|
| HF | USB | Set the dial to the listed frequency. SSTV frequencies are quoted as the suppressed-carrier (dial) frequency, unlike radiofax, so the video sits at 1200–2300 Hz audio. Common: 14.230 MHz (also 14.233), 7.171 MHz, 3.730 MHz, 21.340 MHz, 28.680 MHz. |
| VHF | NFM | 144.500 MHz (Europe), and ISS/ARISS events on 145.800 MHz (usually PD 120, sometimes Robot 36 or PD 180). |

Keep the receiver's squelch open or low on VHF. A squelch that closes between lines looks like a dropout.

The registry default bandwidth is 3 kHz. A tuning error of up to about ±150 Hz is measured from the header and compensated. Beyond that, the header is not recognised.

## Supported modes

Sync pulses are 1200 Hz. Video runs from 1500 Hz (black) to 2300 Hz (white). Line time is measured sync to sync.

| Mode | VIS | Size | Line (ms) | Sync (ms) | Scan per channel (ms) | Colour / order |
|---|---|---|---|---|---|---|
| Robot 36 | 8 | 320x240 | 150.0 | 9.0 | Y 88, chroma 44 | YCrCb 4:2:0: R-Y on even lines (1500 Hz separator), B-Y on odd lines (2300 Hz separator) |
| Robot 72 | 12 | 320x240 | 300.0 | 9.0 | Y 138, R-Y 69, B-Y 69 | YCrCb 4:2:2 |
| Martin M1 | 44 | 320x256 | 446.446 | 4.862 | 146.432 | G, B, R |
| Martin M2 | 40 | 320x256 | 226.798 | 4.862 | 73.216 | G, B, R |
| Scottie S1 | 60 | 320x256 | 428.22 | 9.0 | 138.240 | G, B, *sync*, R |
| Scottie S2 | 56 | 320x256 | 277.692 | 9.0 | 88.064 | G, B, *sync*, R |
| Scottie DX | 76 | 320x256 | 1050.3 | 9.0 | 345.600 | G, B, *sync*, R |
| PD 50 | 93 | 320x256 | 388.16 | 20.0 | 91.520 | Y(even), R-Y, B-Y, Y(odd); two lines per sync |
| PD 90 | 99 | 320x256 | 703.04 | 20.0 | 170.240 | same |
| PD 120 | 95 | 640x496 | 508.48 | 20.0 | 121.600 | same |
| PD 180 | 96 | 640x496 | 754.24 | 20.0 | 183.040 | same |
| Wraase SC2-180 | 55 | 320x256 | 711.0225 | 5.5225 | 235.0 | R, G, B |

Porches and separators by family:

| Family | Porch / separator |
|---|---|
| Robot | 3 ms 1500 Hz after sync; 4.5 ms separator + 1.5 ms 1900 Hz porch before each chroma scan |
| Martin | 0.572 ms 1500 Hz after sync and after every scan |
| Scottie | 1.5 ms 1500 Hz before each scan |
| PD | 2.08 ms 1500 Hz after sync |
| Wraase | 0.5 ms after sync |

### Colour

- Robot and PD use the paper's BT.601 studio-range equations (Y = 16 + 0.003906 (65.738 R + 129.057 G + 25.064 B), etc.). The decoder applies the matching inverse.
- Robot 36 shares chroma across each line pair. The separator tone (1500 / 2300 Hz) decides whether a line carries R-Y or B-Y, with line parity as the fallback if the separator is unreadable.

## How detection works

1. **Front end.** A 129-tap complex band-pass FIR (950–2550 Hz) yields the analytic signal. Its per-sample phase rotation is the instantaneous frequency, and its running sum is an unwrapped phase.
2. **Header.** A run of at least 150 ms within ±120 Hz of 1900 Hz that ends in a drop to 1200 Hz is a VIS candidate. The nominal leader is 300 ms. The first leader, before the 10 ms break, is also tried and simply fails.
   The leader's mean frequency gives the receiver's tuning offset (accepted up to ±150 Hz), which is subtracted from every tone afterwards. A USB dial a few tens of Hz off therefore still decodes with correct brightness.
3. **VIS.** Each of the ten 30 ms bits (start, 7 data + even parity, stop) is classified over its middle 20 ms with a 3-bin DFT at 1100 / 1200 / 1300 Hz. This is Goertzel-equivalent; 20 ms makes the three bins orthogonal.
   Start and stop must be 1200 Hz, no data bit may be 1200 Hz, parity must be even, and the code must be in the table. Only then is an "SSTV <mode> started" image event emitted. There is no sync-timing fallback when the VIS is missed (see Limitations).
4. **Lines.** Each line's sync pulse is searched in a window (±20 ms for the first line, ±4 ms afterwards) around its predicted position. The search thresholds the box-smoothed frequency at 1350 Hz and takes the pulse's trailing edge (1200 → 1500 Hz porch) as the reference. The leading edge is unusable on the first line, where the stop bit runs straight into the sync.
   An amplitude gate (10 % of the leader's level) rejects "syncs" found in silence.
   Found positions feed a least-squares line fit over the last 64 syncs. Its slope is the measured line period, so a sample-clock error on either side shows up as `slant_ppm` and is corrected. Each line starts where the fit says, or at the measured sync when the two disagree by more than 1.5 ms.
   Scottie's mid-line sync is handled by giving its G and B scans negative offsets from the sync.
5. **Pixels.** A pixel's value is the mean frequency over its time slot: the phase difference across the slot divided by the slot length, on the unwrapped phase. Slot edges are computed from the fitted line start and period, so any measured line length resamples to the nominal width.
6. **Output.** The PNG is rewritten atomically about every 1.5 s of picture, and a progress event updates the stored start event.
   The picture finishes when all lines are in, when the decoder is stopped, or on "signal lost": no sync pulse for at least 2 s and at least 3 lines. In that case rows after the last seen sync are dropped and the rest is kept as a partial image.
   The decoder then returns to idle and catches the next header.

Throughput: a 38 s Robot 36 transmission decodes in about 0.15 s, and a 128 s PD 120 transmission in about 1.6 s (M-series laptop, numpy only). That is roughly 100–250x real time.

## Verification

- Round trip against an independently written encoder (`tests/decoder/sstv_encoder.py`, timings typed in again from the paper): Robot 36, Robot 72, Martin M1, Scottie S1, PD 120.
- Martin M1 is also verified against a third-party QSSTV recording (`tests/decoder/data/`).
- Martin M2, Scottie S2/DX, PD 50/90/180 and SC2-180 share the same code paths and differ only in table entries. They have no test fixture.

## Limitations

- No FSK-ID or CW-ID decoding (the station ID that follows some pictures).
- No VIS-less detection. A picture whose header was missed (tuned in late, deep fade during the VIS) is not decoded. A sync-timing fallback was planned but is not implemented.
- No 16-bit extended VIS, so MP/ML/MR modes and PD 240/290 are absent.
- Slant correction tracks sync pulses only. There is no manual slant adjustment, no auto-slant from image content, and no re-synchronisation if the fit is lost for longer than the dropout timeout.
- At the 11,025 Hz input rate, a Robot 36 luma pixel spans about 3 samples, a chroma pixel about 1.5 and a PD 120 pixel about 2.1. Chroma is the first to get noisy on weak signals: the test suite's 15 dB SNR case gives a mean absolute error of about 13/255 on Robot 36 against about 6/255 on Martin M1. Single-sample phase slips show as isolated coloured speckles.
- A Robot 36 line whose separator is unreadable falls back to parity, which is wrong if a line was lost earlier. Robot 72 and PD chroma are assumed per the table and not checked against the separator.
- Images are RGB PNGs only. No de-interlacing, line-doubling, sharpening or colour correction is applied.

See also: [decoder-expansion.md](decoder-expansion.md), [receiver-dsp.md](receiver-dsp.md), [validation/](validation/).
