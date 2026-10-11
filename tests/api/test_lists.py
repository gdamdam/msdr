"""Lists (v0.11.0): GET/POST/PATCH/DELETE /api/lists, item membership/order,
the LIBRARY `list` filter and `list_ids` field, list_add/list_remove bulk ops,
saved searches, duplicate grouping and merge (incl. undo of both delete and
merge). Uses the same running_app helper as tests/api/test_app.py."""

from tests.api.test_app import running_app


async def _signal(client, **overrides):
    body = {"name": "Test Signal", "frequency_hz": 100_000_000}
    body.update(overrides)
    resp = await client.post("/api/signals", json=body)
    assert resp.status_code == 200
    return resp.json()


async def _tag(client, name="DX"):
    resp = await client.post("/api/tags", json={"name": name})
    assert resp.status_code == 200
    return resp.json()


async def _list(client, name="Evening SW"):
    resp = await client.post("/api/lists", json={"name": name})
    assert resp.status_code == 200
    return resp.json()


# --- lists CRUD --------------------------------------------------------


async def test_get_lists_includes_builtin_favorites_ordered_by_position():
    async with running_app() as (app, client, adapter):
        resp = await client.get("/api/lists")
        assert resp.status_code == 200
        lists = resp.json()
        assert [row["id"] for row in lists] == ["favorites"]
        assert lists[0] == {
            "id": "favorites",
            "name": "Favorites",
            "builtin": True,
            "position": 0,
            "count": 0,
        }


async def test_create_list_appends_after_existing():
    async with running_app() as (app, client, adapter):
        created = await _list(client, "Evening SW")
        assert created["builtin"] is False
        assert created["position"] == 1

        names = [row["name"] for row in (await client.get("/api/lists")).json()]
        assert names == ["Favorites", "Evening SW"]


async def test_create_list_name_conflicts_case_insensitively():
    async with running_app() as (app, client, adapter):
        await _list(client, "Evening SW")
        resp = await client.post("/api/lists", json={"name": "evening sw"})
        assert resp.status_code == 409

        resp = await client.post("/api/lists", json={"name": "favorites"})
        assert resp.status_code == 409


async def test_create_list_name_length_validation():
    async with running_app() as (app, client, adapter):
        assert (await client.post("/api/lists", json={"name": ""})).status_code == 422
        assert (
            await client.post("/api/lists", json={"name": "x" * 61})
        ).status_code == 422


async def test_patch_list_renames_and_moves():
    async with running_app() as (app, client, adapter):
        lst = await _list(client, "Evening SW")
        resp = await client.patch(f"/api/lists/{lst['id']}", json={"name": "Night SW"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Night SW"

        resp = await client.patch(f"/api/lists/{lst['id']}", json={"position": 0})
        assert resp.json()["position"] == 0


async def test_patch_list_rejects_name_conflict():
    async with running_app() as (app, client, adapter):
        a = await _list(client, "A")
        await _list(client, "B")
        resp = await client.patch(f"/api/lists/{a['id']}", json={"name": "b"})
        assert resp.status_code == 409


async def test_delete_list_is_blocked_for_builtin():
    async with running_app() as (app, client, adapter):
        resp = await client.delete("/api/lists/favorites")
        assert resp.status_code == 409


async def test_delete_custom_list_removes_it_and_cascades_items():
    async with running_app() as (app, client, adapter):
        lst = await _list(client, "Evening SW")
        signal = await _signal(client)
        await client.post(f"/api/lists/{lst['id']}/items", json={"ids": [signal["id"]]})

        resp = await client.delete(f"/api/lists/{lst['id']}")
        assert resp.status_code == 200
        assert (await client.get("/api/lists")).json() == [
            {"id": "favorites", "name": "Favorites", "builtin": True, "position": 0, "count": 0}
        ]


async def test_unknown_list_operations_404():
    async with running_app() as (app, client, adapter):
        assert (await client.patch("/api/lists/nope", json={"name": "X"})).status_code == 404
        assert (await client.delete("/api/lists/nope")).status_code == 404
        assert (await client.get("/api/lists/nope/items")).status_code == 404
        assert (
            await client.post("/api/lists/nope/items", json={"ids": ["x"]})
        ).status_code == 404


# --- items: add/remove/order --------------------------------------------


async def test_add_items_appends_and_skips_existing_and_unknown():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")
        b = await _signal(client, name="B")

        resp = await client.post(
            "/api/lists/favorites/items", json={"ids": [a["id"], "missing", a["id"]]}
        )
        assert resp.status_code == 200
        assert resp.json() == {"added": 1}

        resp = await client.post("/api/lists/favorites/items", json={"ids": [a["id"], b["id"]]})
        assert resp.json() == {"added": 1}  # a already a member

        items = (await client.get("/api/lists/favorites/items")).json()
        assert [i["name"] for i in items] == ["A", "B"]


async def test_remove_items_route_and_skips_non_members():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")
        b = await _signal(client, name="B")
        await client.post("/api/lists/favorites/items", json={"ids": [a["id"], b["id"]]})

        resp = await client.post(
            "/api/lists/favorites/items/remove", json={"ids": [a["id"], "missing"]}
        )
        assert resp.status_code == 200
        assert resp.json() == {"removed": 1}

        items = (await client.get("/api/lists/favorites/items")).json()
        assert [i["name"] for i in items] == ["B"]


async def test_set_order_requires_exact_permutation():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")
        b = await _signal(client, name="B")
        await client.post("/api/lists/favorites/items", json={"ids": [a["id"], b["id"]]})

        resp = await client.put(
            "/api/lists/favorites/order", json={"ids": [b["id"], a["id"]]}
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        items = (await client.get("/api/lists/favorites/items")).json()
        assert [i["name"] for i in items] == ["B", "A"]

        # Missing an id, extra unknown id, and a duplicate are all rejected.
        assert (
            await client.put("/api/lists/favorites/order", json={"ids": [a["id"]]})
        ).status_code == 422
        assert (
            await client.put(
                "/api/lists/favorites/order", json={"ids": [a["id"], b["id"], "extra"]}
            )
        ).status_code == 422
        assert (
            await client.put(
                "/api/lists/favorites/order", json={"ids": [a["id"], a["id"]]}
            )
        ).status_code == 422


async def test_list_items_route_returns_signals_in_list_order():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A", frequency_hz=100_000_000)
        b = await _signal(client, name="B", frequency_hz=200_000_000)
        await client.post("/api/lists/favorites/items", json={"ids": [b["id"], a["id"]]})

        items = (await client.get("/api/lists/favorites/items")).json()
        assert [i["name"] for i in items] == ["B", "A"]
        assert items[0]["list_ids"] == ["favorites"]


# --- LIBRARY: `list` filter, default order, list_ids field --------------


async def test_library_list_filter_defaults_to_list_position_order():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A", frequency_hz=300_000_000)
        b = await _signal(client, name="B", frequency_hz=100_000_000)
        c = await _signal(client, name="C", frequency_hz=200_000_000)
        # Membership order: b, a -- not frequency or creation order.
        await client.post("/api/lists/favorites/items", json={"ids": [b["id"], a["id"]]})

        resp = await client.get("/api/library", params={"list": "favorites"})
        data = resp.json()
        assert data["total"] == 2
        assert [i["name"] for i in data["items"]] == ["B", "A"]
        assert all(c["id"] != i["id"] for i in data["items"])


async def test_library_list_filter_with_explicit_sort_overrides_list_order():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A", frequency_hz=300_000_000)
        b = await _signal(client, name="B", frequency_hz=100_000_000)
        await client.post("/api/lists/favorites/items", json={"ids": [a["id"], b["id"]]})

        resp = await client.get(
            "/api/library", params={"list": "favorites", "sort": "frequency"}
        )
        assert [i["name"] for i in resp.json()["items"]] == ["B", "A"]


async def test_library_items_carry_list_ids():
    async with running_app() as (app, client, adapter):
        lst = await _list(client, "Evening SW")
        a = await _signal(client, name="A")
        await client.post("/api/lists/favorites/items", json={"ids": [a["id"]]})
        await client.post(f"/api/lists/{lst['id']}/items", json={"ids": [a["id"]]})

        resp = await client.get("/api/library")
        item = next(i for i in resp.json()["items"] if i["id"] == a["id"])
        assert set(item["list_ids"]) == {"favorites", lst["id"]}


# --- bulk list_add / list_remove ----------------------------------------


async def test_bulk_list_add_and_remove():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")
        b = await _signal(client, name="B")

        resp = await client.post(
            "/api/library/bulk",
            json={"ids": [a["id"], b["id"]], "op": "list_add", "value": "favorites"},
        )
        assert resp.status_code == 200
        assert resp.json()["affected"] == 2
        assert "Favorites" in resp.json()["undo"]["label"]

        stored = (await client.get(f"/api/signals/{a['id']}")).json()
        assert stored["list_ids"] == ["favorites"]

        resp = await client.post(
            "/api/library/bulk",
            json={"ids": [a["id"]], "op": "list_remove", "value": "favorites"},
        )
        assert resp.json()["affected"] == 1
        stored = (await client.get(f"/api/signals/{a['id']}")).json()
        assert stored["list_ids"] == []


async def test_bulk_list_op_unknown_list_is_404():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")
        resp = await client.post(
            "/api/library/bulk",
            json={"ids": [a["id"]], "op": "list_add", "value": "no-such-list"},
        )
        assert resp.status_code == 404


# --- saved searches -------------------------------------------------------


async def test_saved_search_crud_and_param_validation():
    async with running_app() as (app, client, adapter):
        assert (await client.get("/api/saved-searches")).json() == []

        resp = await client.post(
            "/api/saved-searches",
            json={"name": "Evening SW", "params": {"q": "radio", "sort": "name"}},
        )
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["name"] == "Evening SW"
        assert saved["params"] == {"q": "radio", "sort": "name"}

        listed = (await client.get("/api/saved-searches")).json()
        assert [s["id"] for s in listed] == [saved["id"]]

        resp = await client.delete(f"/api/saved-searches/{saved['id']}")
        assert resp.status_code == 200
        assert (await client.get("/api/saved-searches")).json() == []


async def test_saved_search_rejects_unknown_param_keys():
    async with running_app() as (app, client, adapter):
        resp = await client.post(
            "/api/saved-searches", json={"name": "X", "params": {"bogus": 1}}
        )
        assert resp.status_code == 422


# --- duplicates -----------------------------------------------------------


async def test_duplicates_groups_adjacent_frequencies_chained_within_tolerance():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A", frequency_hz=10_000_000)
        b = await _signal(client, name="B", frequency_hz=10_000_050)  # 50 Hz from A
        c = await _signal(client, name="C", frequency_hz=10_000_100)  # 50 Hz from B, 100 from A
        await _signal(client, name="D", frequency_hz=20_000_000)  # far away, alone

        resp = await client.get("/api/library/duplicates", params={"tolerance_hz": 60})
        assert resp.status_code == 200
        groups = resp.json()["groups"]
        assert len(groups) == 1
        assert [s["name"] for s in groups[0]] == ["A", "B", "C"]
        assert {a["id"], b["id"], c["id"]} == {s["id"] for s in groups[0]}


async def test_duplicates_separates_by_demodulator():
    async with running_app() as (app, client, adapter):
        await _signal(client, name="AM", frequency_hz=10_000_000, demodulator="AM")
        await _signal(client, name="FM", frequency_hz=10_000_010, demodulator="FM")

        resp = await client.get("/api/library/duplicates", params={"tolerance_hz": 100})
        assert resp.json()["groups"] == []


# --- merge ------------------------------------------------------------


async def test_merge_unions_fields_lists_and_repoints_journal():
    async with running_app() as (app, client, adapter):
        tag_a = await _tag(client, "DX")
        tag_b = await _tag(client, "SWL")
        lst = await _list(client, "Evening SW")

        keep = await _signal(
            client,
            name="Keep",
            frequency_hz=10_000_000,
            tag_ids=[tag_a["id"]],
            station="",
            notes="first note",
            urls=["http://a"],
        )
        other = await _signal(
            client,
            name="Other",
            frequency_hz=10_000_100,
            tag_ids=[tag_b["id"]],
            station="Radio X",
            notes="second note",
            urls=["http://b"],
        )
        await client.post("/api/lists/favorites/items", json={"ids": [other["id"]]})
        await client.post(f"/api/lists/{lst['id']}/items", json={"ids": [keep["id"]]})
        # A journal entry naming `other` (PATCH .../identified writes one).
        await client.patch(f"/api/signals/{other['id']}", json={"status": "identified"})
        journal_before = (await client.get("/api/journal")).json()
        assert any(e["signal_id"] == other["id"] for e in journal_before)

        resp = await client.post(
            "/api/library/merge", json={"keep_id": keep["id"], "merge_ids": [other["id"]]}
        )
        assert resp.status_code == 200
        merged = resp.json()
        assert merged["id"] == keep["id"]
        assert set(merged["tag_ids"]) == {tag_a["id"], tag_b["id"]}
        assert merged["station"] == "Radio X"
        assert merged["notes"] == "first note\nsecond note"
        assert set(merged["urls"]) == {"http://a", "http://b"}
        keep_after = (await client.get(f"/api/signals/{keep['id']}")).json()
        assert set(keep_after["list_ids"]) == {"favorites", lst["id"]}

        assert (await client.get(f"/api/signals/{other['id']}")).status_code == 404

        journal_after = (await client.get("/api/journal")).json()
        assert not any(e["signal_id"] == other["id"] for e in journal_after)
        assert any(e["signal_id"] == keep["id"] for e in journal_after)

        # --- undo restores both signals, their lists, and the journal link.
        undo = await client.post("/api/library/undo")
        assert undo.status_code == 200
        assert "merge" in undo.json()["undone"]

        restored_keep = (await client.get(f"/api/signals/{keep['id']}")).json()
        assert restored_keep["tag_ids"] == [tag_a["id"]]
        assert restored_keep["notes"] == "first note"
        assert restored_keep["list_ids"] == [lst["id"]]

        restored_other = await client.get(f"/api/signals/{other['id']}")
        assert restored_other.status_code == 200
        assert restored_other.json()["tag_ids"] == [tag_b["id"]]
        assert restored_other.json()["list_ids"] == ["favorites"]

        journal_restored = (await client.get("/api/journal")).json()
        matching = [e for e in journal_restored if e["signal_id"] == other["id"]]
        assert matching and all(e["signal_name"] == "Other" for e in matching)
        assert not any(e["signal_id"] == keep["id"] for e in journal_restored)


async def test_merge_requires_another_existing_signal():
    async with running_app() as (app, client, adapter):
        keep = await _signal(client, name="Keep")
        resp = await client.post(
            "/api/library/merge", json={"keep_id": keep["id"], "merge_ids": ["missing"]}
        )
        assert resp.status_code == 422

        resp = await client.post(
            "/api/library/merge", json={"keep_id": "missing", "merge_ids": [keep["id"]]}
        )
        assert resp.status_code == 404


# --- undo of delete restores list memberships ---------------------------


async def test_undo_of_delete_restores_list_memberships():
    async with running_app() as (app, client, adapter):
        lst = await _list(client, "Evening SW")
        signal = await _signal(client, name="A")
        await client.post("/api/lists/favorites/items", json={"ids": [signal["id"]]})
        await client.post(f"/api/lists/{lst['id']}/items", json={"ids": [signal["id"]]})

        await client.delete(f"/api/signals/{signal['id']}")
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404

        resp = await client.post("/api/library/undo")
        assert resp.json()["undone"] == "delete A"

        restored = (await client.get(f"/api/signals/{signal['id']}")).json()
        assert set(restored["list_ids"]) == {"favorites", lst["id"]}


async def test_undo_of_add_to_list_removes_the_membership():
    async with running_app() as (app, client, adapter):
        signal = await _signal(client, name="A")
        await client.post("/api/lists/favorites/items", json={"ids": [signal["id"]]})

        undo = (await client.get("/api/library/undo")).json()["undo"]
        assert "Favorites" in undo["label"]

        resp = await client.post("/api/library/undo")
        assert resp.status_code == 200

        stored = (await client.get(f"/api/signals/{signal['id']}")).json()
        assert stored["list_ids"] == []
