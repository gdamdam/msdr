"""S1 (docs/epics/listening-workbench/S1-ui-contract.md): a settings.json saved
by v0.25 -- four workspaces, no "review", columns without the scan/decode/
review/library ids, inbox arranged in listen -- must load unchanged in intent:
no validation error, a default "review" workspace filled in, and every
SECTION_ID placed in every workspace's Layout (backfill) so nothing becomes
unreachable. Where an id actually renders is WORKSPACE_SECTIONS' call, so
inbox now belongs to discover even though the old file placed it in listen."""

import json

from msdr.settings import SECTION_IDS, WORKSPACE_IDS, WORKSPACE_SECTIONS, Settings

_V025_OLD_IDS = [
    "survey", "nearby", "inbox", "recent", "listen", "log",
    "passes", "sky", "scans", "catalog", "beacons", "finds",
]


def _v025_settings() -> dict:
    listen = {
        "columns": [
            ["survey", "nearby", "listen", "log"],
            ["inbox", "recent", "catalog"],
            ["passes", "sky", "scans", "beacons", "finds"],
        ],
        "wide": ["survey"],
    }

    def other(first: list[str], wide: list[str]) -> dict:
        rest = [sid for sid in _V025_OLD_IDS if sid not in first]
        return {"columns": [first, [], rest], "wide": wide}

    return {
        "workspace": "library",
        "layout": listen,
        "workspaces": {
            "listen": listen,
            "discover": other(["survey", "finds", "scans", "beacons"], ["survey", "finds"]),
            "satellites": other(["passes", "sky"], []),
            "library": other(["catalog"], []),
        },
    }


def test_v025_settings_json_loads_and_backfills_every_workspace(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_v025_settings()))

    loaded = Settings.load(path)

    assert set(loaded.workspaces) == set(WORKSPACE_IDS)
    # The missing workspace gets its own S1 default, not listen's.
    assert loaded.workspaces["review"].columns[0] == ["review"]
    assert loaded.workspaces["review"].wide == ["review"]
    for ws in WORKSPACE_IDS:
        placed = [sid for col in loaded.workspaces[ws].columns for sid in col]
        assert sorted(placed) == sorted(SECTION_IDS), ws
    # The old arrangement is kept; the new ids land at the end of the last column.
    assert loaded.workspaces["listen"].columns[0] == ["survey", "nearby", "listen", "log"]
    assert loaded.workspaces["listen"].columns[2][-5:] == [
        "scan", "decode", "review", "library", "allocations",
    ]
    # Active workspace and its legacy mirror survive the load.
    assert loaded.workspace == "library"
    assert loaded.layout == loaded.workspaces["library"]


def test_inbox_renders_under_discover_after_migration(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_v025_settings()))
    loaded = Settings.load(path)

    # Still stored in listen's columns (never lose an arrangement) ...
    assert "inbox" in loaded.workspaces["listen"].columns[1]
    # ... but the UI renders a workspace's WORKSPACE_SECTIONS only.
    assert "inbox" in WORKSPACE_SECTIONS["discover"]
    assert "inbox" not in WORKSPACE_SECTIONS["library"]
    assert "inbox" not in WORKSPACE_SECTIONS["listen"]
    assert "library" in WORKSPACE_SECTIONS["library"]
    assert {"scan"} <= set(WORKSPACE_SECTIONS["discover"])
    assert {"decode"} <= set(WORKSPACE_SECTIONS["listen"])


def test_v025_settings_json_round_trips_after_migration(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_v025_settings()))
    loaded = Settings.load(path)
    loaded.save(path)
    assert Settings.load(path).workspaces == loaded.workspaces
