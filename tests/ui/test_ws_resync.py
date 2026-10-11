# ruff: noqa: E501 -- embedded JavaScript source lines
"""A "resync" ws message (Broadcaster.next_outgoing: a non-coalescable event
like decoder_event/journal/scan_hit was dropped from a full per-client queue)
must make app.js reload the histories those event types normally patch in
locally, rather than leaving the UI quietly behind. Several resync markers
(or reconnects) arriving in a burst -- the same overflow condition can repeat
for several publishes in a row -- must still only trigger one reload."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_resync_message_triggers_one_debounced_reload_of_the_histories():
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  // Strip both single-line and multi-line `import ...;` statements -- app.js
  // has several of the latter (named-import blocks spanning lines).
  .replace(/^import[\s\S]*?;\n/gm, '')
  .replace('export class MsdrApp', 'class MsdrApp');

const calls = [];
const api = {
  listDecodeEvents: async () => { calls.push('decodeEvents'); return [{ i: 1 }]; },
  journalDays: async () => { calls.push('journalDays'); return { days: ['2026-10-02'] }; },
  journalDay: async (day) => { calls.push('journalDay:' + day); return [{ day }]; },
  inbox: async () => { calls.push('inbox'); return [{ i: 2 }]; },
  recent: async () => { calls.push('recent'); return [{ i: 3 }]; },
  scannerStatus: async () => { calls.push('scannerStatus'); return { state: 'idle', hits: [] }; },
};
const MsdrApp = vm.runInNewContext(src + '\nMsdrApp', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  api, console, setTimeout, clearTimeout, setInterval, clearInterval,
});

// Built via Object.create (not `new`), same as the other app.js/component
// tests in this suite -- the constructor pulls in imports we didn't stub
// (resolveWorkspaceLayout, todayUtc, ...) that _resyncAfterEventLoss itself
// never touches.
const view = Object.create(MsdrApp.prototype);
view.journalDay = '2026-10-02';
view._resyncTimer = null;
view.decodeEvents = [];
view.journal = [];
view.journalDays = [];
view.inbox = [];
view.recent = [];
view.scan = { state: 'idle' };
view.error = null;
view.lastAction = null;

// Three resync markers arrive back to back (the condition that produced the
// first can easily repeat for the next few publishes) -- must still debounce
// to a single reload.
view._onWsMessage({ type: 'resync', reason: 'events_dropped', dropped: 3 });
view._onWsMessage({ type: 'resync', reason: 'events_dropped', dropped: 1 });
view._onWsMessage({ type: 'resync', reason: 'events_dropped', dropped: 2 });

assert.equal(calls.length, 0, 'reload must be debounced, not immediate');

await new Promise((resolve) => setTimeout(resolve, 400));

const expected = ['decodeEvents', 'journalDays', 'journalDay:2026-10-02', 'inbox', 'recent', 'scannerStatus'];
for (const name of expected) {
  const count = calls.filter((c) => c === name).length;
  assert.equal(count, 1, `${name} must be reloaded exactly once, got ${count} (calls: ${calls})`);
}
assert.equal(calls.length, expected.length, `no extra reloads beyond the expected set (calls: ${calls})`);

assert.equal(view.decodeEvents.length, 1, 'decodeEvents replaced from the reload');
assert.deepEqual(view.journalDays, ['2026-10-02']);
assert.ok(view.lastAction && /missed/.test(view.lastAction), 'footer note shown via the existing _action mechanism');
assert.equal(view.error, null, 'the note is informational, not an error');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reconnect_after_the_first_also_triggers_one_reload():
    """connectWs's onOpen(isReconnect) fires true from the second successful
    open on -- a reconnect can cover the same kind of event gap a resync
    marker announces, just without one ever arriving (the socket was simply
    down while the server published events, so there was no queue to
    overflow in the first place)."""
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import[\s\S]*?;\n/gm, '')
  .replace('export class MsdrApp', 'class MsdrApp');

const calls = [];
const api = {
  listDecodeEvents: async () => { calls.push('decodeEvents'); return []; },
  journalDays: async () => { calls.push('journalDays'); return { days: [] }; },
  journalDay: async () => { calls.push('journalDay'); return []; },
  inbox: async () => { calls.push('inbox'); return []; },
  recent: async () => { calls.push('recent'); return []; },
  scannerStatus: async () => { calls.push('scannerStatus'); return { state: 'idle' }; },
};
const MsdrApp = vm.runInNewContext(src + '\nMsdrApp', {
  LitElement: class {}, html: () => {}, customElements: { define() {} },
  api, console, setTimeout, clearTimeout, setInterval, clearInterval,
});

const view = Object.create(MsdrApp.prototype);
view.journalDay = '2026-10-02';
view._resyncTimer = null;
view.decodeEvents = []; view.journal = []; view.journalDays = [];
view.inbox = []; view.recent = []; view.scan = { state: 'idle' };
view.error = null; view.lastAction = null;

// Mirrors the onOpen callback wired up in connectedCallback: isReconnect is
// false for the very first open, true from then on.
const onOpen = (isReconnect) => { if (isReconnect) view._resyncAfterEventLoss(); };
onOpen(false);  // first connect: no reload
await new Promise((resolve) => setTimeout(resolve, 400));
assert.equal(calls.length, 0, 'the first connect must not trigger a reload');

onOpen(true);  // reconnect: one reload
await new Promise((resolve) => setTimeout(resolve, 400));
assert.equal(calls.length, 6, `reconnect must trigger exactly one reload pass (calls: ${calls})`);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr
