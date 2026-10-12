# ruff: noqa: E501 -- embedded JavaScript source lines
"""Decoder UI categories accept API metadata and cover known protocols."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DECODER_INFO = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "decoder-info.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_decoder_categories_and_help():
    module_url = json.dumps(DECODER_INFO.as_uri())
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            f"""
            import {{ decoderCategory, decoderDescription, decoderValidation, groupDecoders }} from {module_url};
            const cases = [
              [{{id: 'pocsag'}}, 'Paging'],
              [{{id: 'dmr', kind: 'call'}}, 'Digital voice'],
              [{{id: 'adsb', kind: 'text'}}, 'Aviation surveillance'],
              [{{id: 'future', kind: 'image'}}, 'Facsimile'],
              [{{id: 'custom', category: 'Custom'}}, 'Custom'],
            ];
            for (const [decoder, expected] of cases) {{
              if (decoderCategory(decoder) !== expected) throw Error(JSON.stringify(decoder));
            }}
            const help = decoderDescription({{id: 'packet9600'}});
            if (!help.includes('IQ') || !help.includes('flat discriminator')) throw Error(help);
            const groups = groupDecoders([{{id: 'pocsag'}}, {{id: 'flex'}}, {{id: 'cw'}}]);
            if (groups.length !== 1 || groups[0].category !== 'Experimental' || groups[0].decoders.length !== 3) {{
              throw Error(JSON.stringify(groups));
            }}
            const evidenceGroups = groupDecoders([
              {{id: 'pocsag', readiness: {{validation: {{state: 'live_output_observed'}}}}}},
              {{id: 'future', category: 'Aviation', readiness: {{validation: {{state: 'untested'}}}}}},
              {{id: 'ft8', readiness: {{validation: {{state: 'reference_iq_tested'}}}}}},
              {{id: 'dmr', readiness: {{validation: {{state: 'field_observed'}}}}}},
              {{id: 'fresh', category: 'HF text', readiness: {{
                validation: {{state: 'reference_iq_tested'}}, validation_stale: true
              }}}},
            ]);
            // live output counts as non-experimental, like reference/field evidence.
            if (evidenceGroups.map(g => g.category).join(',') !== 'Paging,Experimental,HF weak-signal modes,Digital voice') {{
              throw Error(JSON.stringify(evidenceGroups));
            }}
            const refLabel = decoderValidation({{readiness: {{validation: {{state: 'reference_iq_tested'}}}}}});
            if (refLabel.text !== 'reference IQ-tested' || !refLabel.title.includes('off-air performance may still be unverified')) {{
              throw Error(JSON.stringify(refLabel));
            }}
            const fieldLabel = decoderValidation({{readiness: {{validation: {{state: 'field_observed'}}}}}});
            if (fieldLabel.text !== 'off-air observed' || !fieldLabel.title.includes('independent reference')) {{
              throw Error(JSON.stringify(fieldLabel));
            }}
            const liveLabel = decoderValidation({{readiness: {{validation: {{state: 'live_output_observed'}}}}}});
            if (liveLabel.text !== 'live output' || !liveLabel.title.includes('comparison is pending')) {{
              throw Error(JSON.stringify(liveLabel));
            }}
            const metadata = decoderDescription({{
              id: 'future', description: 'Registry supplied help'
            }});
            if (metadata !== 'Registry supplied help') throw Error(metadata);
            """,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_decoder_status_icons_use_three_test_levels():
    module_url = json.dumps(DECODER_INFO.as_uri())
    script = f"""
    import {{ decoderStatusIcons }} from {module_url};
    const r = (validation, extra = {{}}) => ({{ readiness: {{
      installation: {{state: 'builtin'}}, validation: {{state: validation}}, session: {{state: 'idle'}}, ...extra }} }});
    const cases = [
      [r('untested'), '○'],
      [r('fixture_tested'), '◐'],
      [r('reference_iq_tested'), '◐'],
      [r('live_output_observed'), '●'],
      [r('field_observed'), '●'],
      [r('field_observed', {{validation_stale: true}}), '◐'],
      [r('regression_detected'), '✗'],
    ];
    for (const [d, want] of cases) {{
      const got = decoderStatusIcons(d).tested.sym;
      if (got !== want) throw new Error(`${{d.readiness.validation.state}} stale=${{!!d.readiness.validation_stale}}: ${{got}} != ${{want}}`);
    }}
    const stale = decoderStatusIcons(r('field_observed', {{validation_stale: true}})).tested.title;
    if (!stale.includes('older msdr version') || stale.includes('stale')) throw new Error(stale);
    const s = decoderStatusIcons({{ readiness: {{ installation: {{state: 'missing'}}, validation: {{state: 'untested'}},
      session: {{state: 'valid_frames', valid_frames: 3}} }} }});
    if (s.installed.sym !== '✗' || s.session.sym !== '●' || !s.session.title.includes('3 valid frames')) throw new Error(JSON.stringify(s));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
