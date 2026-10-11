# Offline RFML validation

Offline checks for whether ML detections beat DSP, for anyone preparing evidence for the RF ML release gate.

- `msdr-rfml baseline` scores saved DSP and ML detections against explicit operator frequency/time annotations on the **same recording**. It runs no inference and does not infer truth from either detector.
- `msdr-rfml release-gate` combines this report with the existing classifier evaluation.
- Both commands print JSON.

```sh
msdr-rfml baseline detection-manifest.json > detection-report.json
msdr-rfml evaluate classification-corpus.jsonl > classification-report.json
msdr-rfml release-gate detection-manifest.json \
  --classification-report classification-report.json > gate.json
```

Detection manifest schema version 1:

```json
{
  "schema_version": 1,
  "sessions": [
    {
      "id": "held-out-001",
      "recording": "capture-001.npy",
      "split": "test",
      "annotation_complete": true,
      "duration_s": 600,
      "frequency_range_hz": [159000000, 161000000],
      "truth": [
        {"low_hz": 159900000, "high_hz": 160100000, "start_s": 1, "end_s": 5}
      ],
      "dsp": [
        {"source": "dsp", "low_hz": 159900000, "high_hz": 160100000,
         "start_s": 1, "end_s": 5, "score": 0.8}
      ],
      "ml": [
        {"source": "ml", "low_hz": 159950000, "high_hz": 160050000,
         "start_s": 1, "end_s": 5, "score": 0.7}
      ],
      "combined": [
        {"source": "combined", "low_hz": 159900000, "high_hz": 160100000,
         "start_s": 1, "end_s": 5, "score": 0.8}
      ]
    }
  ]
}
```

### Session rules

- Each session needs a unique ID, a recording identifier, `development` or `test` split, complete human annotation, duration, observed frequency range, and truth, DSP, and ML arrays.
- `combined` is optional for diagnostic baseline scoring, but required in **every test session** for the release gate. It must contain the saved outputs of the actual proposed DSP+ML strategy, using `source: "combined"`; an empty array is a valid zero-detection output.
- Empty truth arrays explicitly mark negative sessions; empty prediction arrays are valid.
- Candidate bounds must be finite, increasing, and within the session. Prediction scores are source-native and uncalibrated.
- The recording identifier is provenance, not a file path opened by this command; prepare predictions independently and preserve the original recordings and annotation protocol for audit.
- All candidates in a session must use seconds relative to the same recording start.

### Matching and report

- Matching is greedy by descending frequency/time rectangle IoU with a threshold of 0.1. Each truth and each prediction can match once.
- Unmatched predictions are false positives; unmatched truth regions are false negatives.
- The report includes precision, recall, false positives per recording minute, mean/minimum matched boundary IoU, and individual matches for DSP, ML, diagnostic fusion, and explicit combined output in each split. Undefined ratios are JSON `null`.
- The gate compares combined output with DSP on the held-out test split.

### Classifier rows

The classifier JSONL rows accepted by `msdr-rfml evaluate` may include `"session_id": "held-out-001"` and `"split": "test"` alongside the existing `path`, `label`, and `sample_rate_hz` fields. For the gate:

- **every** row must have both provenance fields;
- all splits must be `test`;
- there must be at least two independent session IDs.

The evaluation report exposes `provenance_complete`, `evaluation_sessions`, and `evaluation_splits`.

### Release gate

The gate returns `passed`, `status`, `observed`, `thresholds`, and `reasons`. `passed=true` means only a **provisional** evidence gate passed.

| Requirement | Threshold |
|---|---|
| Held-out test sessions | at least 5 |
| Annotated regions | at least 30 |
| Fully annotated negative sessions | 2, covering ten minutes |
| DSP false positives | at least 10 |
| Development sessions | at least 1 |
| Recording IDs | no overlap between splits |
| False positives, combined vs DSP | reduced by at least 30% |
| Recall, combined vs DSP | lose no more than five percentage points |
| Classifier errors (`msdr-rfml evaluate`) | zero |
| Accepted known examples | at least 30 |
| Rejection examples | at least 100 |
| False accepts among evaluated rejection examples | at most 5% |
| `selective_accuracy` among accepted known examples | at least 90% |
| Acceptance coverage among known examples | at least 50% |

- The 100-example floor keeps a zero-false-accept result from resting on a handful of negatives; it does not establish a confidence bound unless examples are independently sampled.
- All rejection and acceptance counts must agree with the reported rate, and missing metrics fail closed.
- Missing or insufficient evidence yields `passed=false` with reasons and command exit code 2; malformed input exits 1.
- The gate does not establish representative field performance or verify operator annotations; separate recordings and careful annotation are essential.
- Real-time performance is outside this gate and must be measured separately.


## Live detector windows and coverage (0.46.0)

The detector uses `survey.ml.detector.window_duration_ms` (default 100 ms, allowed 1–250 ms), sized from the configured IQ sample rate. At 6 MS/s this means 600,000 complex samples.

- Old saved `window_samples` values are discarded; 100 ms is used unless the new duration is explicitly configured.
- Gaps, retunes and source changes invalidate partial windows and obsolete results.

### Detector coverage control

The Survey ML controls offer **Detector coverage**:

| Mode | Interval |
|---|---|
| Low CPU (default) | 2 s |
| Balanced | 0.5 s |
| Higher coverage | 0.25 s |

- The interval is the minimum wait after submitting one window before starting the next capture; capturing the next 100 ms takes additional time. It is not a guaranteed inference rate.
- Choices persist and apply to the running detector. Existing interval settings are preserved; the default remains 2 s until the operator selects another mode.
- The queue remains bounded and drops work rather than blocking IQ processing.

### Coverage metrics

| Field | Meaning |
|---|---|
| `sample_coverage` | Samples copied into detector capture |
| `inference_coverage` | Samples in completed, current-generation inference |
| `missed_temporal_fraction` | One minus inferred coverage, including skipped, failed, queued and incomplete work; shown in the UI as **IQ not inferred**. Does not measure the fraction of RF events missed |
| `processing_duty` | Completed inference wall time divided by elapsed observation time, not CPU utilization; process CPU is measured separately in benchmarks |

- `sample_coverage` and `inference_coverage` both divide by IQ samples delivered to detector capture since its last invalidation, excluding upstream losses.
- Frequency coverage describes the latest window's tiles and is a different axis.

### Duplicate suppression and grouping

- Detector duplicate suppression keeps the strongest same-class overlapping box.
- Survey groups repeated frequency regions before selecting at most 16 proposals; it retains separate member time intervals (up to 32, with the full count) and never fills temporal gaps to claim continuous activity.
- Frequency bounds and raw score remain those of the strongest member, not a combined probability.
- This grouping is for inspection, not a new validated detector or truth label.

See also: [rf-ml.md](rf-ml.md), [rf-ml-feasibility.md](rf-ml-feasibility.md), [validation/](validation/).
