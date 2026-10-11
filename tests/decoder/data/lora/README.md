# LoRa reference IQ vectors

## Provenance

All IQ in `vectors.npz` is **synthetic**, generated locally with the real
[gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr) GNU Radio 3.10
out-of-tree module (`lora_sdr_lora_tx`). No off-air or third-party capture is
included. gr-lora_sdr is GPL-3.0 licensed; see `manifest.json`'s
`environment` block for the exact package version/commit used to generate
this set, and `THIRD_PARTY_NOTICES.md` at the repo root for the project-wide
license notice.

These vectors exist to validate an independent, from-scratch native (numpy)
LoRa decoder against the reference implementation's own modulator. They are
not meant to be realistic over-the-air captures: there is no channel noise,
frequency/timing offset, or fading -- only the TX waveform plus zero padding.

## Regeneration

Requires the `.venv-lora` GNU Radio 3.10 + gr-lora_sdr runtime (see
`scripts/setup_lora_runtime.sh`). GNU Radio needs real shared memory for its
buffer allocator; it will fail with `gr::vmcircbuf_sysconfig` /
`shmat: Operation not permitted` inside a sandboxed shell that denies
`shmat`/`shm_open`/`mmap` -- run it in a normal (non-sandboxed) terminal.

```
.venv-lora/bin/python scripts/make_lora_vectors.py
```

This overwrites `vectors.npz` and `manifest.json` in place.

## Files

- `vectors.npz` -- one array per case, keyed by the case `id` used in
  `manifest.json`. Each array is a flat `int8` buffer of interleaved I/Q
  samples: `[I0, Q0, I1, Q1, ...]`.
- `manifest.json` -- per-case TX parameters and metadata, described below.

## `manifest.json` fields

Top level:

- `generated_by`, `generation_command` -- how the file was produced.
- `environment` -- `gnuradio_version`, `lora_sdr_version` (+ build/channel),
  and `python_version` of the generating runtime.
- `archive_file`, `archive_bytes` -- the companion `.npz` and its size.
- `dropped_cases` -- case ids removed to keep the archive under the ~3 MB
  budget (largest/slowest case dropped first). These ids are *not* present
  in `cases`.
- `quantization` -- describes the int8 interleaved layout and how
  `iq_scale` (per case) relates stored samples to the original waveform.

Per case, under `cases[]`:

- `id` -- case identifier; matches the array key in `vectors.npz`.
- `sf`, `bw_hz`, `samp_rate_hz` -- spreading factor and bandwidth; sampling
  rate always equals bandwidth (one sample per chip).
- `cr`, `cr_label` -- coding rate as passed to gr-lora_sdr (`1`..`4`) and the
  Semtech `4/5`..`4/8` label.
- `header` -- `"explicit"` or `"implicit"`.
- `has_crc` -- whether the payload CRC was enabled at TX.
- `ldro_mode`, `ldro_mode_label` -- the raw gr-lora_sdr LDRO parameter
  (`0`=off, `1`=on, `2`=auto) as passed to the TX block.
- `ldro_effective` -- whether LDRO actually applies for this case, per the
  LoRa rule (symbol period > 16 ms), independent of whether `auto` or an
  explicit value was requested.
- `sync_word` -- sync word byte(s) passed to the TX/RX blocks (e.g. `[0x12]`
  for private networks, `[0x2B]` for Meshtastic LongFast).
- `payload_len` -- payload length in bytes.
- `payload_hex` -- the exact transmitted payload, as hex. Payloads are
  derived deterministically from the case `id` (repeated SHA-256; see
  `scripts/make_lora_vectors.py::deterministic_payload`), not from
  Python's `random` module.
- `frame_zero_padd_samples` -- trailing zero samples gr-lora_sdr's TX block
  appended after the frame (its `frame_zero_padd` parameter).
- `leading_zero_samples` -- additional all-zero samples this script
  prepended *outside* the TX flowgraph (~2 symbols).
- `sample_count` -- total complex sample count stored for this case
  (leading zeros + TX output), i.e. half the stored int8 array's length.
- `iq_dtype`, `iq_layout` -- always `"int8"` / interleaved I/Q.
- `iq_scale` -- multiply a stored int8 sample by `1/iq_scale` (or divide by
  `iq_scale`) to recover the real-valued float sample; the waveform was
  scaled so its peak magnitude is ~100.
- `reference_decode` -- whether gr-lora_sdr's own RX (`lora_sdr_lora_rx`),
  fed the dequantized samples, reproduced the exact payload (and, when
  `has_crc` is true, reported a verified CRC). Cases gr-lora_sdr itself
  cannot decode are kept in the set with this set to `false` rather than
  removed or retuned.
- `reference_decode_detail` -- short free-text diagnostic for the above.
- `notes` -- human-readable note on why the case exists.
