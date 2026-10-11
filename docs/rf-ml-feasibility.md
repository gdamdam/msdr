# RF pretrained classifier feasibility

Which pretrained RF models msdr can use without training, for RF ML maintainers.

## Finding

The viable no-training candidate is TorchSig v0.6.1's narrowband XCiT checkpoint, `xcit_narrowband.ckpt`.

- The official v0.6.1 example builds the classifier and loads this pretrained Lightning checkpoint, so using it does not require training.
- Its weights are RF modulation-recognition weights, not ImageNet or random initialization.
- Upstream artifact: [https://bucket.ltsnet.net/torchsig/models/xcit_narrowband.ckpt](https://bucket.ltsnet.net/torchsig/models/xcit_narrowband.ckpt).
- It is a useful frozen baseline; it predates the current TorchSig 2.x data/model APIs.

## Model and input contract

The model is `XCiTClassifier` from TorchSig v0.6.1, implemented as a 1D XCiT transformer.

- It replaces image patch embedding with a 1D convolutional downsampler and uses the transformer class token plus a 1×1 `Conv1d` class head.
- It returns logits for 61 classes, mapped by the exact ordered `torchsig_signals.class_list` in the v0.6.1 pretrained-model example:

`ook, bpsk, 4pam, 4ask, qpsk, 8pam, 8ask, 8psk, 16qam, 16pam, 16ask, 16psk, 32qam, 32qam_cross, 32pam, 32ask, 32psk, 64qam, 64pam, 64ask, 64psk, 128qam_cross, 256qam, 512qam_cross, 1024qam, 2fsk, 2gfsk, 2msk, 2gmsk, 4fsk, 4gfsk, 4msk, 4gmsk, 8fsk, 8gfsk, 8msk, 8gmsk, 16fsk, 16gfsk, 16msk, 16gmsk, ofdm-64, ofdm-72, ofdm-128, ofdm-180, ofdm-256, ofdm-300, ofdm-512, ofdm-600, ofdm-900, ofdm-1024, ofdm-1200, ofdm-2048, fm, am-dsb-sc, am-dsb, am-lsb, am-usb, lfm_data, lfm_radar, chirpss`.

Input pipeline in the example:

1. 4,096 complex IQ samples per prediction.
2. `RandomPhaseShift(phase_offset=(-1, 1))`.
3. `Normalize(norm=np.inf)`.
4. `ComplexTo2D` — two real-valued channels (I and Q), yielding `[batch, 2, 4096]` float input.

Consequences:

- The model is trained for isolated narrowband signal classification on fixed-length IQ windows; it does not detect or separate arbitrary mixed wideband captures.
- Its output vocabulary is the example's 61-entry `torchsig_signals` list, not a generic protocol list.
- Preserve that exact class ordering when translating logits to labels.

## Runtime and licensing

### Runtime

On a native arm64 Mac (macOS 26.6.2, PyTorch 2.14.0), the CLI ran 20 batch-one predictions on deterministic synthetic noise at 50 kHz:

| Backend | p50 | p95 |
|---|---|---|
| CPU | 56.70 ms | 57.11 ms |
| MPS | 6.92 ms | 7.27 ms |

- Timings include input validation, normalization, device transfer, and synchronized prediction.
- Both backends abstained on the noise input; this is a throughput diagnostic, not an accuracy result.
- A separate comparison found finite outputs and maximum CPU/MPS logit delta 2.86e-6.

### Licensing

- TorchSig v0.6.1 source is MIT-licensed.
- The timm XCiT code carries Apache-2.0 attribution (Ross Wightman); see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
- The hosted checkpoint has no separate license metadata in the example, so the source license does not establish checkpoint redistribution terms.

## Reproducibility gaps

- The downloaded checkpoint was 76,389,542 bytes; its SHA-256 is `c92ee780c080c1a22dabfa0b15049991dee94e6fe840bc0c8376a6485c720e0c`.
- msdr pins this hash and size in its model manifest and verifies both on download and load.
- The optional runtime uses native PyTorch/timm and does not train or fetch weights at install time.
- The model has no explicit noise class and cannot distinguish NFM from WFM.
- There is no representative real RF corpus or validated accuracy result.
- In S0/S1 the classifier is shadow-only: it classifies existing DSP candidate windows; it is not an ML detector, detector fusion, or authoritative identification.

### Newer TorchSig Models repository

- [`torchsig-models`](https://github.com/TorchDSP/torchsig-models) is MIT-licensed and advertises a 24-class `XCiTClassifier`, but its README only shows model construction and no released checkpoint URL.
- The separate GRCon 2026 challenge identifies `xcit_narrowband_v1.0.0.ckpt`, while the exact download URL and compatible preprocessing are not described in the repository README.
- The older hosted checkpoint currently has the more actionable integration path.

## References

- [TorchSig v0.6.1 pretrained-model example](https://github.com/TorchDSP/torchsig/blob/v0.6.1/examples/13_example_pretrained_models.ipynb)
- [TorchSig v0.6.1 XCiT implementation](https://github.com/TorchDSP/torchsig/blob/v0.6.1/torchsig/models/iq_models/xcit/xcit1d.py)
- [TorchSig v0.6.1 license](https://github.com/TorchDSP/torchsig/blob/v0.6.1/LICENSE.md)
- [TorchSig Models](https://github.com/TorchDSP/torchsig-models)

## Next-stage wideband checkpoint verification

The [TorchSig Models v1.0.0 release](https://github.com/TorchDSP/torchsig-models/releases/tag/v1.0.0) supplies a real `yolo11n_wideband_v1.0.0.pt` detector checkpoint (5,464,026 bytes; SHA-256 `30d0b0d5055d806fa01bec1d5d7ca0719ad7bee3ae23662a8ec666bdf7e79194`). This updates the earlier narrowband-first finding: a concrete wideband inference path now exists.

### Model and transform

- Loaded metadata confirms one detector class (`item`), task `detect`, three input image channels, image size 512 and `single_cls=true`. It does **not** identify modulation.
- Exact training FFT/window/normalization parameters are not sufficiently specified, so msdr's transform is versioned and marked experimental.
- The runtime does not import TorchSig or GNU Radio.
- The 0.36 balanced adapter adds adaptive FFT resolution and bounded overlapping frequency tiles; see [RF ML operation](rf-ml.md#balanced-discovery-036). This is a separate experimental preprocessing version, not a claim of compatibility or measured accuracy with the pretrained detector.

### Smoke checks

- Native arm64, Ultralytics 8.4.164, torch 2.14.0, 131,072 complex synthetic IQ samples. CPU and MPS both returned valid detector results with zero candidates.
- CPU first prediction about 514 ms; separate MPS CLI first prediction about 3.35 s, excluding model loading but including first-use backend setup.
- These are cold functional checks, not steady-state throughput comparisons or accuracy evidence. They preceded the optional live shadow integration documented in [rf-ml.md](rf-ml.md).

### Licensing

Ultralytics' [licensing page](https://www.ultralytics.com/license) describes AGPL-3.0/Enterprise licensing for its models and implementation; the TorchDSP repository's MIT license alone must not be interpreted as a weights license.

See also: [rf-ml.md](rf-ml.md), [rfml-validation.md](rfml-validation.md).
