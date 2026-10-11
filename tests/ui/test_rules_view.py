# ruff: noqa: E501 -- embedded JavaScript source lines
"""rules-view.js (docs/rules.md "UI"): the pure helpers behind the watch
rules editor -- kind labels, default config per kind, a human summary line
for the rules table, merging live Wi-Fi/BLE tables + labels into a beacon
picker, and polygon point validation. Same vm.runInNewContext harness as
test_device_history_chart.py/test_ble_view.py."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ui._node_env import node_env

SOURCE = Path(__file__).resolve().parents[2] / "msdr/ui/components/rules-view.js"

PRELUDE = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const raw = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .*;\n/gm, '')
  .replace(/^export (function|class|const) /gm, '$1 ');
const ctx = { LitElement: class {}, html: () => {}, customElements: { define() {} }, console };
"""

EXPORTS = "kindLabel, defaultConfig, summarizeConfig, beaconOptions, validatePolygonPoints"


def _run(body, exports=EXPORTS):
    script = (
        PRELUDE
        + f"const {{ {exports} }} = vm.runInNewContext(raw + '\\n({{ {exports} }})', ctx);\n"
        + body
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(SOURCE)],
        capture_output=True, text=True, timeout=30, env=node_env(),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_kind_label():
    _run(r"""
assert.equal(kindLabel("beacon_presence"), "Beacon presence/absence");
assert.equal(kindLabel("sensor_threshold"), "Sensor threshold");
assert.equal(kindLabel("drone_geofence"), "Drone geofence");
assert.equal(kindLabel("unknown_kind"), "unknown_kind");
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_default_config_shapes():
    _run(r"""
const beacon = defaultConfig("beacon_presence");
assert.equal(beacon.receiver, "ble");
assert.equal(beacon.mode, "seen");

const sensor = defaultConfig("sensor_threshold");
assert.equal(sensor.comparison, "above");

const geofence = defaultConfig("drone_geofence");
assert.equal(geofence.geometry.type, "circle");
assert.equal(geofence.geometry.radius_m, 100);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_summarize_config_per_kind():
    _run(r"""
assert.equal(
  summarizeConfig({ kind: "beacon_presence", config: { receiver: "ble", ident: "c0:11:22:33:44:55", mode: "seen" } }),
  "c0:11:22:33:44:55 seen on ble"
);
assert.equal(
  summarizeConfig({ kind: "beacon_presence", config: { receiver: "wifi", ident: "aa:bb:cc:dd:ee:ff", mode: "missing", missing_after_minutes: 10 } }),
  "aa:bb:cc:dd:ee:ff missing on wifi for 10 min"
);
assert.equal(
  summarizeConfig({ kind: "sensor_threshold", config: { addr: "c0:11:22:33:44:55", sensor_name: "temp_c", comparison: "above", threshold: 30, hysteresis: 2 } }),
  "c0:11:22:33:44:55 temp_c above 30 (±2)"
);
assert.equal(
  summarizeConfig({ kind: "drone_geofence", config: { geometry: { type: "circle", center: { lat: 1, lon: 2 }, radius_m: 500 } } }),
  "inside 500 m of 1.0000, 2.0000"
);
assert.equal(
  summarizeConfig({ kind: "drone_geofence", config: { geometry: { type: "polygon", points: [1, 2, 3] } } }),
  "inside a 3-point area"
);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_beacon_options_merges_wifi_and_ble_filters_private_ble_and_prefers_labels():
    _run(r"""
const wifi = [{ bssid: "aa:bb:cc:dd:ee:ff", ssid: "HomeNet" }];
const ble = [
  { addr: "c0:11:22:33:44:55", identity: "static", name: "Bean" },
  { addr: "4a:34:3f:50:52:88", identity: "rpa", name: "ignored-rpa" },
];
const labels = { "wifi:aa:bb:cc:dd:ee:ff": { name: "Office AP" } };
const options = beaconOptions(wifi, ble, labels);
assert.equal(options.length, 2, "the rpa address must be filtered out");
assert.equal(JSON.stringify(options[0]), JSON.stringify({ receiver: "wifi", ident: "aa:bb:cc:dd:ee:ff", label: "Office AP" }));
assert.equal(JSON.stringify(options[1]), JSON.stringify({ receiver: "ble", ident: "c0:11:22:33:44:55", label: "Bean" }));
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_validate_polygon_points():
    _run(r"""
assert.equal(validatePolygonPoints([{ lat: 0, lon: 0 }, { lat: 1, lon: 1 }]), "A polygon needs at least 3 points.");
assert.equal(
  validatePolygonPoints([{ lat: 0, lon: 0 }, { lat: 1, lon: "x" }, { lat: 2, lon: 2 }]),
  "Every point needs a numeric lat/lon."
);
assert.equal(validatePolygonPoints([{ lat: 0, lon: 0 }, { lat: 1, lon: 1 }, { lat: 2, lon: 2 }]), null);
""")
