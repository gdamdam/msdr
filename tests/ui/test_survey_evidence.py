# ruff: noqa: E501 -- embedded JavaScript source lines
"""Behavioral coverage for the survey's evidence filter and strength order."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EVIDENCE_JS = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui" / "survey-evidence.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_evidence_categories_filtering_and_strength_sort():
    module_url = json.dumps(EVIDENCE_JS.as_uri())
    script = f"""
import {{ surveyEvidence, matchesEvidenceFilter, compareEvidence }} from {module_url};
const cases = [
  [{{ confirmed: {{ decoder: "aprs", integrity: "passed" }} }}, "validated"],
  [{{ confirmed: {{ decoder: "aprs", integrity: "failed" }} }}, "decoder_output"],
  [{{ confirmed: {{ decoder: "aprs" }} }}, "decoder_output"],
  [{{ family: {{ name: "POCSAG" }} }}, "estimate"],
  [{{ catalog: [{{ name: "scheduled" }}] }}, "context"],
  [{{}}, "none"],
  [{{ rds: {{ pi: "0x1234", groups: 2 }} }}, "validated"],
  [{{ rds: {{ pi: "0x1234", groups: 0 }} }}, "decoder_output"],
  [{{ rds: {{ ps: "Station" }} }}, "decoder_output"],
  [{{ confirmed: {{ integrity: "failed" }}, rds: {{ pi: "0x1234", groups: 2 }} }}, "validated"],
  [{{ decoder_candidates: [{{ id: "adsb", evidence_kind: "allocation" }}] }}, "context"],
  [{{ decoder_candidates: [{{ id: "m17", evidence_kind: "iq_estimate" }}] }}, "estimate"],
];
for (const [label, expected] of cases) {{
  const actual = surveyEvidence(label).category;
  if (actual !== expected) throw new Error(`${{actual}} != ${{expected}}`);
  if (!matchesEvidenceFilter(label, expected) || matchesEvidenceFilter(label, "other")) {{
    throw new Error(`filter mismatch for ${{expected}}`);
  }}
}}
if (!(compareEvidence(cases[0][0], cases[1][0], "strongest") < 0)) throw new Error("validated must sort first");
if (!(compareEvidence(cases[5][0], cases[4][0], "weakest") < 0)) throw new Error("none must sort first when weakest");
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_source_badges_preserve_uncertainty():
    script = f"""
import {{ evidenceBadges, surveyEvidence }} from {json.dumps(EVIDENCE_JS.as_uri())};
import assert from 'node:assert/strict';
const label = {{snr_db: 12, bandplan: {{name:'Marine'}}, catalog:[{{name:'Scheduled'}}],
 confirmed:{{decoder:'dsc', integrity:'failed'}}}};
const badges = evidenceBadges(label);
assert.deepEqual(badges.map(b=>b.source), ['dsp','allocation','catalog','decoded']);
assert.equal(badges.find(b=>b.source==='decoded').symbol, '⚠');
assert.match(badges.find(b=>b.source==='catalog').title, /not evidence/);
assert.equal(evidenceBadges({{confirmed:{{}}}})[0].symbol, '?');
assert.equal(evidenceBadges({{confirmed:{{integrity:'passed'}}}})[0].symbol, '✓');
assert.equal(evidenceBadges({{rds:{{pi:'1234',groups:2}}}})[0].symbol, '✓');
assert.deepEqual(evidenceBadges({{claims:[{{evidence_type:'operator',superseded_by:'next'}}]}}), []);
assert.equal(evidenceBadges({{claims:[{{evidence_type:'operator'}}]}})[0].source, 'operator');
// Labels stored by the removed experimental RF ML feature carry rfml fields;
// they must neither crash nor render a badge.
assert.deepEqual(evidenceBadges({{rfml:{{status:'ok',classification:'FSK',score:.95}},
  rfml_detection:{{score:.72,stale:false}}}}), []);
assert.equal(surveyEvidence({{rfml:{{status:'ok',classification:'FSK'}}}}).category, 'none');
assert.deepEqual(evidenceBadges({{analysis_state:{{state:'waiting'}}}}), []);

"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_dense_marker_cells():
    script = f"""
import {{ evidenceBadges, clusteredEvidenceMarkers }} from {json.dumps(EVIDENCE_JS.as_uri())};
import assert from 'node:assert/strict';
const labels = [
  {{frequency_hz:100e6,snr_db:8}},
  {{frequency_hz:100e6+500,snr_db:9,bandplan:{{name:'Allocation'}}}},
  {{frequency_hz:100e6+1000,confirmed:{{integrity:'passed'}}}},
];
const markers = clusteredEvidenceMarkers(labels,99.5e6,1e6,600);
assert.equal(markers.length,3);
assert.deepEqual(markers.flatMap(m=>m.anchors).map(x=>Number(x.toFixed(6))), [300,300.3,300.6]);
assert.equal(markers[0].x,300);
assert.ok(markers[1].x - markers[0].x >= 28);
assert.ok(markers[2].x - markers[1].x >= 28);
assert.deepEqual(markers[2].sources.map(s=>s.source), ['decoded']);
const single = clusteredEvidenceMarkers([labels[0]],99.5e6,1e6,600);
assert.equal(single[0].x,300, 'isolated marker stays exactly on frequency');
const dense = clusteredEvidenceMarkers(labels,99.5e6,1e6,28);
assert.equal(dense.length,1);
assert.equal(dense[0].anchors.length,3);
assert.deepEqual(dense[0].sources.map(s=>s.source), ['dsp','allocation','decoded']);
assert.equal(clusteredEvidenceMarkers(labels,0,0,600).length,0);
const edge = clusteredEvidenceMarkers([
  {{frequency_hz:1246,snr_db:1}}, {{frequency_hz:1260,snr_db:1}}
], 0, 1261, 1261);
assert.equal(edge.length, 2);
assert.deepEqual(edge.flatMap(m=>m.anchors), [1246,1260]);
assert.ok(edge[1].x <= 1261-14);
assert.ok(edge[1].x-edge[0].x >= 28);
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
