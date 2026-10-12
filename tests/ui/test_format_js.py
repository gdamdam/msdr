# ruff: noqa: E501 -- embedded JavaScript source lines
"""parseFrequencyInput (msdr/ui/format.js) has no JS test runner in this repo
-- run it for real via `node --input-type=module`, skipping if node isn't
installed rather than failing the suite over an environment gap."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FORMAT_JS = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui" / "format.js"
COMPONENTS_DIR = FORMAT_JS.parent / "components"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_CASES = [
    ["14.100", 14_100_000],
    ["14100", 14_100_000],
    ["7.2M", 7_200_000],
    ["7.2 MHz", 7_200_000],
    ["145800k", 145_800_000],
    ["145800 kHz", 145_800_000],
    ["9000000 Hz", 9_000_000],
    ["", None],
    ["not a frequency", None],
    ["-5", None],
    ["0", None],
]

_SCRIPT = """
import {{ parseFrequencyInput }} from {module_url};

const cases = {cases};
const failures = [];
for (const [input, expected] of cases) {{
  const got = parseFrequencyInput(input);
  if (got !== expected) {{
    const shown = JSON.stringify(input);
    failures.push(`parse(${{shown}}) = ${{got}}, expected ${{expected}}`);
  }}
}}
if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""


def test_parse_frequency_input_cases():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _SCRIPT.format(module_url=module_url, cases=json.dumps(_CASES))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# relativeOrDate (library-view.js's LAST HEARD column): `at` is fixed and
# injected so this doesn't depend on when the test runs. Cases at/past the
# 24h boundary assert only that the result *looks* like a plain date --
# dateTime() reads the instant in local time, which would make an exact
# string flaky under a different TZ.
_AT_ISO = "2026-01-02T12:00:00Z"
_RELATIVE_CASES = [
    [None, "--"],
    ["not a date", "--"],
    ["2026-01-02T11:59:40Z", "just now"],
    ["2026-01-02T11:55:00Z", "5m ago"],
    ["2026-01-02T10:00:00Z", "2h ago"],
]
_DATE_SHAPED_CASES = [
    "2026-01-01T10:00:00Z",  # 26h before `at` -- past the relative window
    "2026-01-03T00:00:00Z",  # in the future
]

_RELATIVE_SCRIPT = """
import {{ relativeOrDate }} from {module_url};

const at = new Date({at_iso});
const exactCases = {exact_cases};
const dateShapedCases = {date_shaped_cases};
const datePattern = /^\\d{{4}}-\\d{{2}}-\\d{{2}}$/;
const failures = [];
for (const [input, expected] of exactCases) {{
  const got = relativeOrDate(input, at);
  if (got !== expected) {{
    const shown = `relativeOrDate(${{JSON.stringify(input)}}) = ${{JSON.stringify(got)}}`;
    failures.push(`${{shown}}, expected ${{JSON.stringify(expected)}}`);
  }}
}}
for (const input of dateShapedCases) {{
  const got = relativeOrDate(input, at);
  if (!datePattern.test(got)) {{
    const shown = `relativeOrDate(${{JSON.stringify(input)}}) = ${{JSON.stringify(got)}}`;
    failures.push(`${{shown}}, expected a YYYY-MM-DD date`);
  }}
}}
if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""


def test_relative_or_date_cases():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _RELATIVE_SCRIPT.format(
        module_url=module_url,
        at_iso=json.dumps(_AT_ISO),
        exact_cases=json.dumps(_RELATIVE_CASES),
        date_shaped_cases=json.dumps(_DATE_SHAPED_CASES),
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# readinessLabels (decode-deck.js's ⚙ modal decoder list): three separate
# facts -- never one collapsed "available" verdict -- plus the pre-readiness
# fallback (available/install_hint only) for a backend that hasn't shipped
# `readiness` yet.
_READINESS_CASES = [
    # no readiness at all -- falls back to available/install_hint
    [
        {"available": False, "install_hint": "pip install dsd-fme"},
        {"installed": "missing", "tested": "--", "session": "--"},
    ],
    [{"available": True}, {"installed": "installed", "tested": "--", "session": "--"}],
    # installed with version, untested, idle session
    [
        {
            "readiness": {
                "installation": {"state": "present", "version": "1.2.3"},
                "validation": {"state": "untested"},
                "session": {"state": "idle"},
                "validation_stale": False,
                "usable": True,
            }
        },
        {"installed": "installed v1.2.3", "tested": "untested", "session": "idle"},
    ],
    # stale reference-IQ validation, session with events and valid frames
    [
        {
            "readiness": {
                "installation": {"state": "builtin"},
                "validation": {"state": "reference_iq_tested"},
                "session": {"state": "valid_frames", "events": 3, "valid_frames": 12},
                "validation_stale": True,
                "usable": True,
            }
        },
        {
            "installed": "built-in",
            "tested": "reference IQ-tested (stale)",
            "session": "valid frames (3 ev, valid frames 12)",
        },
    ],
    # incompatible install, regression detected
    [
        {
            "readiness": {
                "installation": {"state": "incompatible", "install_hint": "upgrade dsd-fme"},
                "validation": {"state": "regression_detected"},
                "session": {"state": "error"},
                "validation_stale": False,
                "usable": False,
            }
        },
        {"installed": "incompatible", "tested": "REGRESSION", "session": "error"},
    ],
]

_READINESS_SCRIPT = """
import {{ readinessLabels }} from {module_url};

const cases = {cases};
const failures = [];
for (const [decoder, expected] of cases) {{
  const got = readinessLabels(decoder);
  const actual = {{
    installed: got.installed.text, tested: got.tested.text, session: got.session.text,
  }};
  const gotJson = JSON.stringify(actual);
  const expJson = JSON.stringify(expected);
  if (gotJson !== expJson) {{
    const shown = JSON.stringify(decoder);
    failures.push(`readinessLabels(${{shown}}) = ${{gotJson}}, expected ${{expJson}}`);
  }}
}}
if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""


def test_readiness_labels_cases():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _READINESS_SCRIPT.format(module_url=module_url, cases=json.dumps(_READINESS_CASES))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# format.js's section / workspace tables must mirror msdr/settings.py exactly -- a drift would make
# the UI render a workspace's sections differently from what the backend
# validates and backfills.
_WORKSPACE_SCRIPT = """
import * as f from {module_url};
console.log(JSON.stringify({{
  SECTION_IDS: f.SECTION_IDS,
  WORKSPACE_IDS: f.WORKSPACE_IDS,
  WORKSPACE_SECTIONS: f.WORKSPACE_SECTIONS,
  DEFAULT_WORKSPACE_COLUMNS: f.DEFAULT_WORKSPACE_COLUMNS,
  DEFAULT_WORKSPACE_WIDE: f.DEFAULT_WORKSPACE_WIDE,
  labels: Object.keys(f.SECTION_LABELS),
  wsLabels: Object.keys(f.WORKSPACE_LABELS),
  wsHelp: f.WORKSPACE_HELP,
}}));
"""


def test_workspace_tables_mirror_settings_py():
    from msdr import settings as s

    module_url = json.dumps(FORMAT_JS.as_uri())
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _WORKSPACE_SCRIPT.format(module_url=module_url)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    js = json.loads(result.stdout)
    assert js["SECTION_IDS"] == list(s.SECTION_IDS)
    assert js["WORKSPACE_IDS"] == list(s.WORKSPACE_IDS)
    assert js["WORKSPACE_SECTIONS"] == {k: list(v) for k, v in s.WORKSPACE_SECTIONS.items()}
    assert sorted(js["labels"]) == sorted(s.SECTION_IDS)
    assert js["wsLabels"] == list(s.WORKSPACE_IDS)
    # Every workspace tab has hover help (data-help on the tab button).
    assert list(js["wsHelp"]) == list(s.WORKSPACE_IDS)
    assert all(text.strip() for text in js["wsHelp"].values())
    # Defaults compared pre-backfill: listen's is the bare Layout() literal.
    assert js["DEFAULT_WORKSPACE_COLUMNS"]["listen"] == s.Layout.model_fields[
        "columns"
    ].default_factory()
    for ws in s.WORKSPACE_IDS:
        if ws == "listen":
            continue
        assert js["DEFAULT_WORKSPACE_COLUMNS"][ws] == s._WORKSPACE_DEFAULT_COLUMNS[ws]
        assert js["DEFAULT_WORKSPACE_WIDE"][ws] == s._WORKSPACE_DEFAULT_WIDE[ws]
    assert js["DEFAULT_WORKSPACE_WIDE"]["listen"] == s.Layout().wide


# evidence/observation "state" replaces confidence percentages/meters everywhere --
# these regexes catch the two shapes a reintroduced percentage would take
# (`Math.round(...confidence...100)` and `confidence * 100`). survey-view.js
# already renders a unitless `score` (not confidence*100) and is outside
# this test's scope, so it's scanned too, but a match there is reported, not silently
# fixed here.
_CONFIDENCE_PERCENT_RES = [
    re.compile(r"Math\.round\([^)]*confidence[^)]*100"),
    re.compile(r"confidence\s*\*\s*100"),
]


def test_no_percentage_confidence_rendering():
    files = sorted(COMPONENTS_DIR.glob("*.js")) + [FORMAT_JS]
    offenders = []
    for path in files:
        text = path.read_text()
        for rx in _CONFIDENCE_PERCENT_RES:
            for m in rx.finditer(text):
                offenders.append(f"{path.name}: {m.group(0)!r}")
    assert not offenders, "confidence percentage rendering found:\n" + "\n".join(offenders)


_EVIDENCE_STATE_LABEL_SCRIPT = """
import {{ evidenceStateLabel }} from {module_url};

const cases = {cases};
const failures = [];
for (const [input, expected] of cases) {{
  const got = evidenceStateLabel(input);
  if (got !== expected) {{
    const shown = JSON.stringify(input);
    const gotS = JSON.stringify(got), expS = JSON.stringify(expected);
    failures.push(`evidenceStateLabel(${{shown}}) = ${{gotS}}, expected ${{expS}}`);
  }}
}}
if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""

_EVIDENCE_STATE_LABEL_CASES = [
    ["context", "context"],
    ["estimated", "estimated"],
    ["validated", "validated"],
    ["operator", "identified by operator"],
    [None, ""],
    ["unknown_state", "unknown_state"],
]


def test_evidence_state_label_cases():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _EVIDENCE_STATE_LABEL_SCRIPT.format(
        module_url=module_url, cases=json.dumps(_EVIDENCE_STATE_LABEL_CASES)
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# integrityBadge -- badge text is never a correctness word like "available"/"ready", and the
# tooltip is always the raw integrity_note (never rewritten). "passed" picks
# the check name out of the note (FCS/CRC/...) when one is named, else a
# generic "checked".
_INTEGRITY_BADGE_SCRIPT = """
import {{ integrityBadge }} from {module_url};

const cases = {cases};
const failures = [];
for (const [integrity, note, expected] of cases) {{
  const got = integrityBadge(integrity, note);
  const actual = {{ text: got.text, title: got.title }};
  const gotJson = JSON.stringify(actual);
  const expJson = JSON.stringify(expected);
  if (gotJson !== expJson) {{
    failures.push(`integrityBadge(${{JSON.stringify(integrity)}}, ${{JSON.stringify(note)}}) = ${{gotJson}}, expected ${{expJson}}`);
  }}
}}
if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""

_INTEGRITY_BADGE_CASES = [
    ["passed", "AX.25 FCS verified by direwolf", {"text": "✓ FCS", "title": "AX.25 FCS verified by direwolf"}],
    ["passed", "CRC-16/X.25 verified", {"text": "✓ CRC", "title": "CRC-16/X.25 verified"}],
    ["passed", "builtin range check ok", {"text": "✓ checked", "title": "builtin range check ok"}],
    ["passed", "", {"text": "✓ checked", "title": ""}],
    ["failed", "AX.25 FCS mismatch", {"text": "✗ failed", "title": "AX.25 FCS mismatch"}],
    ["unavailable", "multimon exposes no flag", {"text": "no check", "title": "multimon exposes no flag"}],
    ["not_applicable", "", {"text": "—", "title": ""}],
    ["unknown", "", {"text": "unverified", "title": ""}],
    [None, None, {"text": "unverified", "title": ""}],
]


def test_integrity_badge_cases():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _INTEGRITY_BADGE_SCRIPT.format(module_url=module_url, cases=json.dumps(_INTEGRITY_BADGE_CASES))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# sessionFactText -- decode-deck's per-decoder session line. Never
# "available"/"ready"; unverified_output/valid_frames carry their counts,
# unsupported_input carries the reason.
_SESSION_FACT_TEXT_SCRIPT = """
import {{ sessionFactText }} from {module_url};

const cases = {cases};
const failures = [];
for (const [facts, expected] of cases) {{
  const got = sessionFactText(facts);
  if (got !== expected) {{
    failures.push(`sessionFactText(${{JSON.stringify(facts)}}) = ${{JSON.stringify(got)}}, expected ${{JSON.stringify(expected)}}`);
  }}
}}
if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""

_SESSION_FACT_TEXT_CASES = [
    [{"session_state": "acquiring"}, "acquiring"],
    [{"session_state": "unverified_output", "events": 4}, "4 events (unverified)"],
    [{"session_state": "valid_frames", "valid_frames": 12}, "12 valid frames"],
    [{"session_state": "synchronized"}, "synchronized"],
    [{"session_state": "quiet"}, "quiet"],
    [{"session_state": "unsupported_input", "unsupported": "input rate mismatch"}, "unsupported input: input rate mismatch"],
    [{"session_state": "error"}, "error"],
    [{"session_state": "idle"}, "idle"],
    [{}, "idle"],
]


def test_session_fact_text_cases():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _SESSION_FACT_TEXT_SCRIPT.format(module_url=module_url, cases=json.dumps(_SESSION_FACT_TEXT_CASES))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


# callLineText must round duration_s -- the backend sends the raw float, which
# rendered as "⏱ 8.978223085403442s" in decode rows.
_CALL_LINE_TEXT_SCRIPT = """
import {{ callLineText }} from {module_url};
import assert from "node:assert/strict";

const ev = {{ fields: {{ tgt: 65535, src: 1209, protocol: "DMR", color_code: "01", duration_s: 8.978223085403442 }} }};
assert.equal(callLineText(ev), "TG 65535 · from 1209 · DMR · CC 01 · ⏱ 9.0s");
"""


def test_call_line_text_rounds_duration():
    module_url = json.dumps(FORMAT_JS.as_uri())
    script = _CALL_LINE_TEXT_SCRIPT.format(module_url=module_url)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_decode_chip_label():
    script = f"""
import {{ decodeChipLabel }} from {json.dumps(FORMAT_JS.as_uri())};
import assert from 'node:assert/strict';
assert.equal(decodeChipLabel(null), null);
assert.equal(decodeChipLabel({{ decoder: {{ active: false }} }}), null);
assert.equal(decodeChipLabel({{ decoder: {{ active: true, decoders: [{{ id: 'cw' }}] }} }}), 'DECODE · CW');
assert.equal(decodeChipLabel({{ decoder: {{ active: false, band: {{ active: true, decoders: ['adsb'] }} }} }}), 'DECODE · ADSB');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
