# LoRa receive from SDRconnect IQ

msdr's optional `lora` raw-IQ decoder uses the upstream GNU Radio OOT module
[`gr-lora_sdr`](https://github.com/tapparelj/gr-lora_sdr) for CSS demodulation,
header decoding, deinterleaving, FEC, whitening, and payload CRC. It runs on the
live SDRconnect IQ stream and recorded IQ in Review. It is receive-only.

## Install and validate the PHY runtime

The Apple Silicon conda package currently targets Python 3.12, while the ordinary
checkout may use Python 3.13. From this checkout, use the isolated recipe in
[`environment-lora.yml`](../../../environment-lora.yml):

```sh
brew install micromamba       # macOS, if needed
scripts/setup_lora_runtime.sh
.venv-lora/bin/msdr
```

The script installs msdr into `.venv-lora/` and runs
[`scripts/validate_lora_iq.py`](../../../scripts/validate_lora_iq.py). That check
transmits a synthetic LoRa packet through upstream `gr-lora_sdr` and decodes
SDRconnect-format int16 IQ through msdr, requiring a verified PHY CRC and an exact
payload match. GNU Radio and its native LoRa module must live in the same Python
environment as msdr. The ordinary `.venv/bin/msdr` remains separate and may still
report LoRa as unavailable. On other platforms, use the upstream
[`gr-lora_sdr` installation guide](https://github.com/tapparelj/gr-lora_sdr#installation)
if the pinned conda package cannot be resolved.

Verified here on macOS arm64 with Python 3.12.14, GNU Radio 3.10.11 and the
2026-01-05 `gr-lora_sdr` conda build. This is synthetic IQ validation, not an
off-air reception claim.

## Tune and configure

Select **LoRa / Meshtastic / MeshCore** in the decoder list and tune SDRconnect
to the signal. In Setup, choose the protocol and match spreading factor,
bandwidth, and coding-rate index to the transmitter. Defaults are SF11,
250 kHz, and coding-rate index 1 (4/5), matching Meshtastic LongFast. IQ sample
rate and tuned channel must cover the selected bandwidth; the decoder rejects
out-of-span configurations. Meshtastic uses sync word `0x2B`; generic LoRa and
MeshCore currently use private sync word `0x12`.

The PHY backend only emits packets with an explicit `crc_valid=true` tag from
`gr-lora_sdr`. The protocol parser reports Meshtastic outer headers and leaves
their application bytes opaque. MeshCore version-1 headers, route, path, and
payload type are exposed; bodies are retained as bytes. Decryption is not
implemented. In particular, msdr does not claim that opaque payloads are
encrypted when the packet alone cannot prove that.

The tests exercise stream framing and protocol-header parsing. The synthetic
upstream TX-to-msdr RX loopback passed with the environment above. This repository
does not include a known LoRa IQ reference recording, and no off-air RF
validation has been completed. Correct reception depends on accurate frequency,
bandwidth, spreading factor, coding rate, and a supported sync word.
