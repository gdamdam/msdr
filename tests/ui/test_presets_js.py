# ruff: noqa: E501 -- embedded JavaScript source lines
"""PRESETS (msdr/ui/presets.js) has no JS test runner in this repo -- run it
for real via `node --input-type=module`, skipping if node isn't installed
rather than failing the suite over an environment gap. Modelled on
test_format_js.py."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PRESETS_JS = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui" / "presets.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_VALID_WORKSPACES = ["listen", "discover", "review"]

_SCRIPT = """
import {{ PRESETS }} from {module_url};

const validWorkspaces = new Set({valid_workspaces});
const failures = [];

if (!Array.isArray(PRESETS) || PRESETS.length === 0) {{
  failures.push("PRESETS must be a non-empty array");
}} else {{
  const seenIds = new Set();
  for (const p of PRESETS) {{
    const shown = JSON.stringify(p?.id ?? p);
    if (!p || typeof p.id !== "string" || !p.id) failures.push(`${{shown}}: missing/invalid id`);
    if (!p || typeof p.label !== "string" || !p.label) failures.push(`${{shown}}: missing/invalid label`);
    if (!p || !validWorkspaces.has(p.workspace)) {{
      failures.push(`${{shown}}: workspace ${{JSON.stringify(p?.workspace)}} not in listen/discover/review`);
    }}
    if (!p || !Array.isArray(p.plan) || p.plan.length === 0 || p.plan.some((line) => typeof line !== "string" || !line)) {{
      failures.push(`${{shown}}: plan must be a non-empty array of non-empty strings`);
    }}
    if (p?.id) {{
      if (seenIds.has(p.id)) failures.push(`duplicate preset id ${{JSON.stringify(p.id)}}`);
      seenIds.add(p.id);
    }}
  }}
}}

if (failures.length) {{
  console.error(failures.join("\\n"));
  process.exit(1);
}}
"""


def test_every_preset_has_id_label_workspace_and_plan():
    module_url = json.dumps(PRESETS_JS.as_uri())
    script = _SCRIPT.format(module_url=module_url, valid_workspaces=json.dumps(_VALID_WORKSPACES))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_explore_hf_states_field_validation_pending():
    module_url = json.dumps(PRESETS_JS.as_uri())
    script = f"""
import {{ PRESETS }} from {module_url};
const p = PRESETS.find((x) => x.id === "explore-hf");
if (!p) {{ console.error("explore-hf preset not found"); process.exit(1); }}
const joined = p.plan.join(" ");
if (!joined.includes("HF field validation pending") || !joined.includes("synthetic/reference tested only")) {{
  console.error("explore-hf plan missing the field-validation-pending note: " + joined);
  process.exit(1);
}}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
