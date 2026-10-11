"""A slow background load must not undo a completed favorite toggle."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_favorite_toggle_wins_over_pending_refresh():
    source = Path(__file__).resolve().parents[2] / "msdr/ui/components/recent-list.js"
    script = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
let complete;
const api = {listListItems:()=>new Promise(resolve=>{complete=resolve}),
 addListItems:async()=>{}, removeListItems:async()=>{}};
const code=fs.readFileSync(process.argv[1],'utf8')
 .replace(/^import .*;\n/gm,'').replace('export class RecentList','class RecentList');
const RecentList=vm.runInNewContext(code+'\nRecentList',{
 LitElement:class {dispatchEvent(){}},customElements:{define(){}},api,
 CustomEvent:class {constructor(type,init){this.type=type;this.detail=init.detail}}});
const list=new RecentList();list.signals=[{id:'one',name:'Station'}];
const pending=list._loadFavorites();
await list._toggleFavorite({stopPropagation(){}},list.signals[0]);
complete([]);await pending;
assert.equal(list._isFavorite(list.signals[0]),true);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
