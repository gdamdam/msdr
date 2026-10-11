"""Saved frequency matching exercised in Node, matching the UI's ES module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HELPER_JS = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "saved-frequency.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(script):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_saved_frequency_matches_tolerance_favorites_modes_and_deduplication():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ savedFrequencyMatches }} from {module};
const signals = [
  {{id:"one", name:"Saved channel", frequency_hz:100000000, demodulator:"AM"}},
  {{id:"two", name:"Far channel", frequency_hz:100000002, demodulator:"FM"}},
  {{id:"three", name:"At tolerance", frequency_hz:100000001, demodulator:"NFM"}},
];
const favorites = [
  {{id:"one", name:"Duplicate favorite copy", frequency_hz:100000000, demodulator:"AM"}},
  {{id:"fav-only", name:"Favorite only", frequency_hz:100000000.5, demodulator:"USB"}},
];
const matches = savedFrequencyMatches(100000000, signals, favorites);
if (matches.length !== 3)
  throw Error("frequency tolerance or deduplication mismatch");
if (matches[0].id !== "one" ||
    matches[0].name !== "Saved channel" ||
    matches[0].favorite !== true)
  throw Error("favorite membership or stable signal precedence mismatch");
if (matches[0].demodulator !== "AM" ||
    matches[1].demodulator !== "NFM" ||
    matches[2].demodulator !== "USB")
  throw Error("mode metadata missing or mode mismatch hid a channel");
if (matches.some(m => m.kind !== "signal"))
  throw Error("match kind missing");
if (savedFrequencyMatches(100000000, signals, []).some(m => m.favorite))
  throw Error("non-favorite marked favorite");
if (savedFrequencyMatches(null, signals, favorites).length !== 0)
  throw Error("invalid tuned frequency accepted");
""")



def test_nearest_saved_frequency_is_bounded_and_keeps_exact_receiver_rule():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ nearestSavedFrequency, savedFrequencyMatches }} from {module};
const signals = [
  {{id:"adsb", name:"ADS-B", frequency_hz:1090000000}},
  {{id:"wrong", name:"Other", frequency_hz:1091000000}},
  {{id:"vhf", name:"VHF", frequency_hz:162425000}},
];
const favorite = [{{...signals[0]}}];
const hit = nearestSavedFrequency(1089967000, signals, favorite);
if (hit?.id !== "adsb" || hit.distanceHz !== 33000 || !hit.favorite)
  throw Error("expected the 33 kHz-offset ADS-B memory as the nearest saved channel");
if (savedFrequencyMatches(1089967000, signals, favorite).length)
  throw Error("proximity must not change the receiver's exact saved-channel rule");
if (nearestSavedFrequency(1089949000, signals, favorite) !== null)
  throw Error("match beyond the 50 kHz upper bound");
if (nearestSavedFrequency(162430000, signals)?.id !== "vhf")
  throw Error("close VHF memory was missed");
if (nearestSavedFrequency(162434000, signals) !== null)
  throw Error("distant VHF memory was incorrectly matched");
if (nearestSavedFrequency(null, signals) !== null)
  throw Error("invalid frequency was accepted");
""")


def test_receiver_summary_preserves_aliases_and_prefers_named_favorite():
    module = json.dumps(HELPER_JS.as_uri())
    _run(f"""
import {{ summarizeSavedMatches }} from {module};
import assert from 'node:assert/strict';
const records = [
  {{name:'UNKNOWN 161.975000 MHz', favorite:false, demodulator:'NFM'}},
  {{name:'AIS 1 · 161.975 MHz', favorite:true, demodulator:'NFM'}},
  {{name:'AIS NAVAL 161.975000 MHz', favorite:true, demodulator:'RAW'}},
];
const before = JSON.stringify(records);
const summary = summarizeSavedMatches(records);
assert.equal(summary.name, 'AIS 1');
assert.equal(summary.favorite, true);
assert.deepEqual(summary.modes, ['NFM', 'RAW']);
assert.equal(summary.records, records);
assert.equal(JSON.stringify(records), before);
assert.equal(summarizeSavedMatches([]), null);
assert.equal(summarizeSavedMatches([{{name:null}}]).name, 'Saved channel');
assert.equal(summarizeSavedMatches([{{name:'UNKNOWN 161.975 MHz'}}]).name, 'Saved channel');
assert.equal(summarizeSavedMatches([{{name:'Channel 16'}}]).name, 'Channel 16');
""")
