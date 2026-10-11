# Experimental RF ML

Experimental TorchSig classifier and detector beside the DSP survey, for evaluators. Currently dormant.

## Status: dormant (0.70.0)

The live classifier and detector are switched off for every install. The pretrained models were not useful on real captures. `AVAILABLE = False` in `msdr/rfml/__init__.py`:

- makes Survey ignore saved `survey.ml` settings (kept untouched);
- hides the Survey ML section and the Setup toggle;
- reports `ml_config.available: false` in survey status;
- makes `POST /api/survey/ml` return 404.

Everything else stays: the `msdr/rfml` package, the settings models, the `ml` and `ml-detector` extras, the `msdr-rfml` command for offline use, and the tests (which switch the gate on with the `ml_available` fixture). To bring the feature back with a better model, set `AVAILABLE = True`. The rest of this page describes the feature as it works when enabled.

### What it does when enabled

msdr can run two pretrained TorchSig models beside the DSP survey.

- Both are **opt-in** and run in **shadow mode**. Neither has passed the independent comparison against DSP, so their outputs remain explicitly experimental.
- Classifier and Detector switches and expandable results are shown in Survey by default. Hide them with Setup → Features → Show experimental ML controls; hiding controls does not stop enabled models.
- Offline tools and explicit expert shadow configuration remain available.
- ML never replaces DSP labels or confirms a station or service.

| | Narrowband classifier | Wideband detector |
|---|---|---|
| Model | TorchSig v0.6.1 XCiT `xcit_narrowband.ckpt` | TorchSig Models v1.0.0 YOLO11n wideband |
| Input | Up to three contiguous 4,096-sample channel IQ windows | 65,536-sample wideband IQ window from the analyzer stream |
| Output | Modulation family (AM, FM, FSK, PSK, QAM, OFDM, ASK/PAM, CHIRP) or UNKNOWN | Frequency/time boxes with a raw score in offline/API results |
| Extra | `pip install '.[ml]'` | `pip install '.[ml-detector]'` |
| Weights | `msdr-rfml download` | `msdr-rfml download-detector` |
| Default path | `~/.cache/msdr/models/xcit_narrowband.ckpt` | `~/.cache/msdr/models/yolo-wideband.pt` |

**Validation status:** neither model has been measured on a representative real-capture corpus. Treat scores as uncalibrated model preferences. See [Limits](#limits).

## Install

```sh
python -m pip install '.[ml]'            # classifier (torch, timm)
python -m pip install '.[ml-detector]'   # detector (torch, ultralytics)
msdr-rfml download                       # verifies size and SHA-256, installs atomically
msdr-rfml download-detector
```

Use native arm64 Python 3.11+. Rosetta, Docker and CUDA are not needed. Nothing is downloaded during `pip install`, and the UI never downloads weights. Pass `--model-path PATH` to `download` and `benchmark` to use another location.

Pinned classifier artifact: 76,389,542 bytes, SHA-256 `c92ee780c080c1a22dabfa0b15049991dee94e6fe840bc0c8376a6485c720e0c`. Class order and input contract: [rf-ml-feasibility.md](rf-ml-feasibility.md). Detector provenance: `msdr/rfml/detector.json`.

## Research configuration

Prefer the offline tools below. To collect shadow results deliberately, stop msdr and edit `survey.ml` in `~/Library/Application Support/msdr/settings.json`.

- Both `enabled` and `detector.enabled` default to false.
- Existing explicit settings are preserved; set both false to stop a previously enabled shadow worker.
- Worker state/errors appear beside the Survey switches and remain available in the API.

```json
{
  "survey": {
    "ml": {
      "enabled": false,
      "model_path": "~/.cache/msdr/models/xcit_narrowband.ckpt",
      "device": "auto",
      "score_threshold": 0.70,
      "queue_size": 2,
      "max_job_age_s": 5.0,
      "result_ttl_s": 60,
      "export_dir": "",
      "export_mode": "disagreements",
      "export_max_bytes": 67108864,
      "export_interval_s": 10,
      "detector": {
        "enabled": false,
        "model_path": "~/.cache/msdr/models/yolo-wideband.pt",
        "device": "auto",
        "score_threshold": 0.50,
        "window_samples": 65536,
        "interval_s": 2.0,
        "queue_size": 1,
        "max_job_age_s": 5.0,
        "result_ttl_s": 30,
        "target_bin_hz": 1500,
        "max_tiles": 4
      }
    }
  }
}
```

| Key | Meaning |
|---|---|
| `device` | Live macOS workers use CPU for `auto` and `mps` after a native Metal abort; the UI reports the safety fallback. Offline CLI `auto` picks MPS when available, else CPU; catchable unsupported operations retry on CPU. |
| `score_threshold` | Cutoff on the top raw class score. Not a probability. |
| `queue_size` | Pending inference jobs. Oldest are dropped first. |
| `max_job_age_s` | Queue deadline. A window older than this when dequeued is dropped. Slow inference is still published, counted as `slow`, and shown as stale. |
| `result_ttl_s` | Display lifetime. Classifier results (60 s) are marked stale past it; the last detector result (30 s) stays visible with its capture age until then. |
| `export_*` | Save `.npz` examples for offline evaluation. Empty `export_dir` disables it. `disagreements` keeps DSP/ML conflicts and abstentions; `all` keeps every eligible prediction. Export is rate-limited, stops at the byte quota, and never deletes. |
| `window_samples`, `interval_s` | Detector window length and how often a window is sampled. |
| `target_bin_hz`, `max_tiles` | Adaptive FFT resolution and overlapping 512-bin frequency tiles. One reserved tile rotates over the whole frequency grid independently of DSP targets; remaining tiles follow targets and fill unused positions. FFT is capped at 8192 bins, spectrogram at 512 frames. |

## How it runs

- IQ comes from the analyzer's existing live or replay stream. The DSP survey still finds candidates and owns the accumulator.
- Each model loads once per analyzer lifetime on a dedicated single thread, off the event loop.
- Queues are bounded. Retunes, replay seeks and reported stream gaps invalidate pending ML windows only; the DSP dwell keeps accumulating.
- Age is checked before inference. Results are always published afterwards, with `inference_s` recorded, and a `slow` counter increments when inference exceeded `max_job_age_s`.
- The last detector result is retained in the API for `detector.result_ttl_s`. Its capture age is included, and it is flagged stale once that age passes `max_job_age_s`.
- The classifier takes up to three contiguous 4,096-sample windows around the strongest window of a dwell. Every window must pass its raw-score threshold and agree on the broad family; otherwise it abstains. This may increase abstentions, and needs corpus validation. Narrow ML proposals (up to 25 kHz) can use spare classifier slots after DSP targets.
- Detector boxes keep their proposal id across runs when they overlap the previous box, so observations and classifier results stay attached.
- Missing packages or weights leave DSP running and report an unavailable API status.

## Listening and validation

- DSP estimates and protocol decoder evidence remain visible.
- A passed payload integrity check is stronger evidence than a modulation guess; decoder output without such a check stays tentative.
- **Validation sample** still saves IQ and operator observations for later review, independently of ML.
- Shadow observations remain append-only research evidence with model and preprocessing provenance. Removing their live presentation does not erase old recordings or claims.
- Promotion requires the [release gate](rfml-validation.md).

## Offline tools

```sh
# Latency check on synthetic noise or a saved channel (.npy, 1-D complex)
msdr-rfml benchmark --device cpu --iterations 50 --sample-rate 50000 [--iq channel.npy]

# Detect on saved wideband IQ; optional DSP candidates for fusion
msdr-rfml detect --iq wideband.npy --sample-rate 6000000 --center-frequency 160000000 \
  --target-bin-hz 1500 --max-tiles 4 [--dsp-json candidates.json] > detections.json

# Evaluate exported examples against manually checked labels
msdr-rfml evaluate corpus.jsonl --device cpu > evaluation.json
```

`benchmark` measures throughput, not accuracy. `detect` accepts up to 1,048,576 samples per call; slice longer recordings. Fusion matches DSP candidates on frequency **and** time overlap, keeps DSP boundaries and unmatched DSP detections, and keeps sufficiently scored ML-only boxes.

The evaluation manifest is JSONL with one line per example, labels limited to the eight families plus `NOISE` and `UNKNOWN`:

```json
{"path":"examples/one.npz","label":"FM","sample_rate_hz":50000}
{"path":"noise.npy","label":"NOISE","sample_rate_hz":50000}
```

The report gives a confusion matrix, accepted coverage, known-class accuracy with abstentions, selective accuracy, and false accepts on rejection examples. Inference errors are listed separately and excluded from rejection-rate denominators. Never feed automated predictions back in as labels.

## Limits

- No real-capture accuracy measurement exists yet. HF validation is pending.
- The classifier has no noise or voice class and cannot tell NFM from WFM. UNKNOWN means abstention, not noise.
- The detector image now follows [TorchSig v2.1.1 `compute_spectrogram`](https://github.com/TorchDSP/torchsig/blob/v2.1.1/torchsig/utils/dsp.py) (Blackman power spectrum, reversed frequency rows and time columns) and the [TorchSig Models YOLO image adapter](https://github.com/TorchDSP/torchsig-models/blob/main/torchsig_models/adapters/yolo_utils.py) (per-image normalization and inversion). Its upstream checkpoint training FFT settings are not fully documented, so this versioned transform remains experimental. Finer FFT resolution does not make the pretrained model more accurate.
- Sampled windows can miss bursts. Losses the source does not report cannot be detected. Capture coordinates are upper bounds, not exact window indices.
- No protocol, service or station identity follows from a modulation estimate or a detector box.

## Benchmarks

Apple Silicon, warm runs, synthetic noise. These show native execution and latency only. They do not establish live Metal stability: concurrent live inference aborted the server on 2026-09-28. Live macOS inference now uses CPU; run Metal comparisons in separate CLI processes.

| Path | Input | CPU | Metal (MPS) |
|---|---|---:|---:|
| Detector, single image | 65,536 samples @ 6 MS/s, 10 iterations | 21.2 ms median / 24.7 ms p95 | 9.3 ms / 11.2 ms |
| Detector, 4 adaptive tiles | 65,536 samples, 5 runs | 132 ms median | 54 ms median |

## Licensing

TorchSig v0.6.1 source is MIT. The XCiT backbone includes timm code by Ross Wightman under Apache-2.0. Ultralytics (detector) is AGPL-3.0 or commercial. The hosted checkpoints carry no separate license metadata; the hash pins identify the artifacts, they do not grant redistribution rights. See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Capture validation examples

Open **Validation sample** below the ML strip.

1. Choose **Full visible span** for one bounded source frame or a **1, 3, or 5 second** continuous capture; or choose **Analyzed signal** for up to three contiguous classifier windows.
2. Add your observation and notes, then save.

Requirements and behavior:

- Survey and an IQ analysis dimension must be active; both ML switches can remain off.
- Channel capture follows the next completed analyzer target, not necessarily the VFO.
- Choose **Setup → Recordings → Validation samples folder** to set a separate directory. Empty uses `<recording output folder>/rfml-validation`. Existing files stay in place.
- At 6 MS/s, five seconds contains about 240 MB of IQ before compression.

Each archive contains complex IQ, a programmatic spectrogram and its coordinate metadata, capture duration, frequency/sample rate, session ID, Survey evidence snapshot, and a separate operator annotation.

- Ground truth remains unset. A voice/digital description must not be treated as a confirmed modulation label.
- Unavailable antenna/configuration details are not invented.

Limits:

| Limit | Value |
|---|---|
| Requests | one at a time, 30 seconds waiting |
| Timed wideband samples | at most 64 million |
| Snapshots | 1,048,576 wideband or 12,288 channel samples |
| Directory quota | 512 MiB |

- Gaps and receiver changes reject timed captures.
- Files are saved off the capture loop.
- Long captures retain all IQ; their spectrogram overview samples at most 1,024 time windows, with the actual hop recorded in metadata.

Short snapshots are useful examples, not continuous detection benchmarks. Use longer independently annotated recordings for burst recall and false detections per minute. [Baseline schemas and provisional release gate](rfml-validation.md) describe the next step. No real-capture accuracy improvement is claimed yet.

See also: [rfml-validation.md](rfml-validation.md), [rf-ml-feasibility.md](rf-ml-feasibility.md), [validation/](validation/).
