# ruff: noqa: E501 -- embedded JavaScript source lines
"""decode-view.js holds live messages while the user is inspecting the list
(scrolled, hovering, a fields panel open, or a call playing) and merges them
only on the "N new" pill or once idle at the top -- rows must not move under
the cursor. Run via node with import lines stripped, like
test_review_view_damaged_recording.py."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

DECODE_VIEW = Path(__file__).resolve().parents[2] / "msdr" / "ui" / "components" / "decode-view.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_live_messages_are_held_while_inspecting_and_merged_on_flush():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const strip = (src) => src.replace(/^import[\s\S]*?;\n/gm, '').replace(/^export /gm, '');
const src = strip(fs.readFileSync(process.argv[1], 'utf8'));
const ctx = { LitElement: class {}, html: () => '', customElements: { define() {} }, console };
vm.runInNewContext(src + '\nglobalThis.DecodeView = DecodeView;', ctx);

const panel = { scrollTop: 0, scrollTo({ top }) { this.scrollTop = top; } };
const view = Object.create(ctx.DecodeView.prototype);
Object.assign(view, {
  open: true, _decoder: '', _q: '', _playingId: null, _hovering: false,
  _held: [], _openFields: new Set(), _events: [{ id: 1 }], decodeEvents: [],
  querySelector: () => panel,
});
// vm-realm arrays fail deepEqual against outer-realm literals.
const ids = (list) => Array.from(list, (e) => e.id);
const push = (id) => { view.decodeEvents = [{ id }, ...view.decodeEvents]; view._mergeLive(); };

// Idle at the top: live.
push(2);
assert.deepEqual(ids(view._events), [2, 1]);

// Hovering the rows: held, list unchanged.
view._hovering = true;
push(3);
push(4);
assert.deepEqual(ids(view._events), [2, 1]);
assert.deepEqual(ids(view._held), [4, 3]);

// Still holding because a fields panel is open after the pointer leaves.
view._openFields.add(1);
view._hovering = false;
view._maybeFlush();
assert.equal(view._held.length, 2);

// Closing it releases the hold and merges newest-first.
view._onFieldsToggle({ id: 1 }, { open: false });
assert.deepEqual(ids(view._events), [4, 3, 2, 1]);
assert.equal(view._held.length, 0);

// Scrolled down: held; the pill merges and scrolls to the top.
panel.scrollTop = 300;
push(5);
assert.deepEqual(ids(view._held), [5]);
view._flushHeld({ scrollTop: true });
assert.equal(view._events[0].id, 5);
assert.equal(panel.scrollTop, 0);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(DECODE_VIEW)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
