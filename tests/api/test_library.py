"""LIBRARY view: GET /api/library (query/filter/sort/paging), bulk ops and
the undo log. Uses the same running_app helper as tests/api/test_app.py."""

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


async def test_signal_coordinates_require_a_complete_pair_on_create_and_patch():
    async with running_app() as (app, client, adapter):
        response = await client.post(
            "/api/signals",
            json={
                "name": "One coordinate", "frequency_hz": 100_000_000, "latitude": 37.5
            },
        )
        assert response.status_code == 422

        signal = await _signal(client, latitude=37.5, longitude=-122.2)
        response = await client.patch(f"/api/signals/{signal['id']}", json={"longitude": None})
        assert response.status_code == 422

        response = await client.patch(
            f"/api/signals/{signal['id']}", json={"latitude": None, "longitude": None}
        )
        assert response.status_code == 200
        assert response.json()["latitude"] is None
        assert response.json()["longitude"] is None


# --- query: text/frequency match, filters, sort, paging, total ------------


async def test_library_query_matches_name_station_notes_location():
    async with running_app() as (app, client, adapter):
        await _signal(client, name="Meteor Beacon", frequency_hz=137_100_000)
        await _signal(
            client, name="Weather Fax", frequency_hz=137_500_000, station="Meteostation"
        )
        await _signal(client, name="Aircraft Tower", frequency_hz=120_000_000, notes="met office")
        await _signal(client, name="Other", frequency_hz=145_000_000, location="Metropolis")
        await _signal(client, name="Unrelated", frequency_hz=99_000_000)

        resp = await client.get("/api/library", params={"q": "met"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        assert {i["name"] for i in data["items"]} == {
            "Meteor Beacon",
            "Weather Fax",
            "Aircraft Tower",
            "Other",
        }


async def test_library_query_matches_a_parsed_frequency():
    async with running_app() as (app, client, adapter):
        await _signal(client, name="On freq", frequency_hz=137_100_000)
        await _signal(client, name="Off freq", frequency_hz=200_000_000)

        resp = await client.get("/api/library", params={"q": "137.1"})
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "On freq"


async def test_library_query_filters_status_tag_and_frequency_band():
    async with running_app() as (app, client, adapter):
        tag = await _tag(client)
        await _signal(
            client,
            name="A",
            frequency_hz=100_000_000,
            status="identified",
            tag_ids=[tag["id"]],
        )
        await _signal(client, name="B", frequency_hz=200_000_000, status="unknown")

        resp = await client.get("/api/library", params={"status": "identified"})
        assert {i["name"] for i in resp.json()["items"]} == {"A"}

        resp = await client.get("/api/library", params={"tag": tag["id"]})
        assert {i["name"] for i in resp.json()["items"]} == {"A"}

        resp = await client.get("/api/library", params={"lo_hz": 150_000_000})
        assert {i["name"] for i in resp.json()["items"]} == {"B"}

        resp = await client.get("/api/library", params={"hi_hz": 150_000_000})
        assert {i["name"] for i in resp.json()["items"]} == {"A"}


async def test_library_query_sort_name_and_order():
    async with running_app() as (app, client, adapter):
        await _signal(client, name="Bravo", frequency_hz=100_000_000)
        await _signal(client, name="Alpha", frequency_hz=200_000_000)

        resp = await client.get("/api/library", params={"sort": "name", "order": "asc"})
        assert [i["name"] for i in resp.json()["items"]] == ["Alpha", "Bravo"]

        resp = await client.get("/api/library", params={"sort": "name", "order": "desc"})
        assert [i["name"] for i in resp.json()["items"]] == ["Bravo", "Alpha"]


async def test_library_query_last_heard_sorts_nulls_last():
    async with running_app() as (app, client, adapter):
        never = await _signal(client, name="Never heard", frequency_hz=146_600_000)
        heard = await _signal(client, name="Heard once", frequency_hz=146_520_000)
        # Tuning to it runs touch_heard(), setting last_heard_at; "never" stays NULL.
        resp = await client.post(f"/api/tune/signal/{heard['id']}")
        assert resp.status_code == 200

        for order in ("asc", "desc"):
            resp = await client.get(
                "/api/library", params={"sort": "last_heard", "order": order}
            )
            ids = [i["id"] for i in resp.json()["items"]]
            assert ids == [heard["id"], never["id"]]  # NULL always sorts last


async def test_library_query_paging_and_total():
    async with running_app() as (app, client, adapter):
        for i in range(5):
            await _signal(client, name=f"S{i}", frequency_hz=100_000_000 + i)

        resp = await client.get(
            "/api/library", params={"limit": 2, "offset": 0, "sort": "frequency"}
        )
        data = resp.json()
        assert data["total"] == 5
        assert [i["name"] for i in data["items"]] == ["S0", "S1"]

        resp = await client.get(
            "/api/library", params={"limit": 2, "offset": 4, "sort": "frequency"}
        )
        data = resp.json()
        assert data["total"] == 5
        assert [i["name"] for i in data["items"]] == ["S4"]


async def test_library_query_bad_sort_or_order_is_422():
    async with running_app() as (app, client, adapter):
        assert (await client.get("/api/library", params={"sort": "bogus"})).status_code == 422
        assert (await client.get("/api/library", params={"order": "bogus"})).status_code == 422


async def test_library_query_offset_and_limit_are_bounded():
    async with running_app() as (app, client, adapter):
        assert (await client.get("/api/library", params={"offset": -1})).status_code == 422
        assert (await client.get("/api/library", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/library", params={"limit": 201})).status_code == 422


# --- bulk ops ---------------------------------------------------------------


async def test_bulk_delete_skips_unknown_ids():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")
        b = await _signal(client, name="B")

        resp = await client.post(
            "/api/library/bulk", json={"ids": [a["id"], b["id"], "missing"], "op": "delete"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["affected"] == 2
        assert body["undo"]["label"] == "delete 2 memories"

        assert (await client.get("/api/library")).json()["total"] == 0


async def test_bulk_tag_add_and_remove():
    async with running_app() as (app, client, adapter):
        tag = await _tag(client, "DX")
        a = await _signal(client, name="A")
        b = await _signal(client, name="B")

        resp = await client.post(
            "/api/library/bulk",
            json={"ids": [a["id"], b["id"]], "op": "tag_add", "value": tag["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["undo"]["label"] == "tag +DX on 2 memories"
        stored = (await client.get(f"/api/signals/{a['id']}")).json()
        assert tag["id"] in stored["tag_ids"]

        resp = await client.post(
            "/api/library/bulk", json={"ids": [a["id"]], "op": "tag_remove", "value": tag["id"]}
        )
        assert resp.json()["undo"]["label"] == "tag -DX on 1 memories"
        stored = (await client.get(f"/api/signals/{a['id']}")).json()
        assert tag["id"] not in stored["tag_ids"]


async def test_bulk_set_status_and_color():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")

        resp = await client.post(
            "/api/library/bulk",
            json={"ids": [a["id"]], "op": "set_status", "value": "identified"},
        )
        assert resp.json()["affected"] == 1
        assert (await client.get(f"/api/signals/{a['id']}")).json()["status"] == "identified"

        await client.post(
            "/api/library/bulk", json={"ids": [a["id"]], "op": "set_color", "value": "red"}
        )
        assert (await client.get(f"/api/signals/{a['id']}")).json()["color"] == "red"

        await client.post(
            "/api/library/bulk", json={"ids": [a["id"]], "op": "set_color", "value": None}
        )
        assert (await client.get(f"/api/signals/{a['id']}")).json()["color"] is None


async def test_bulk_validation_errors():
    async with running_app() as (app, client, adapter):
        a = await _signal(client, name="A")

        resp = await client.post(
            "/api/library/bulk",
            json={"ids": [a["id"]], "op": "tag_add", "value": "no-such-tag"},
        )
        assert resp.status_code == 404

        resp = await client.post(
            "/api/library/bulk", json={"ids": [a["id"]], "op": "set_status", "value": "bogus"}
        )
        assert resp.status_code == 422

        resp = await client.post(
            "/api/library/bulk", json={"ids": [a["id"]], "op": "set_color", "value": "bogus"}
        )
        assert resp.status_code == 422

        assert (
            await client.post("/api/library/bulk", json={"ids": [], "op": "delete"})
        ).status_code == 422

        assert (
            await client.post(
                "/api/library/bulk", json={"ids": ["x"] * 5001, "op": "delete"}
            )
        ).status_code == 422


# --- undo --------------------------------------------------------------


async def test_undo_empty_is_null_and_409_on_post():
    async with running_app() as (app, client, adapter):
        assert (await client.get("/api/library/undo")).json() == {"undo": None}
        assert (await client.post("/api/library/undo")).status_code == 409


async def test_undo_of_create_deletes_the_signal():
    async with running_app() as (app, client, adapter):
        signal = await _signal(client, name="A")

        undo = (await client.get("/api/library/undo")).json()["undo"]
        assert undo["label"] == "create A"

        resp = await client.post("/api/library/undo")
        assert resp.status_code == 200
        assert resp.json() == {"undone": "create A", "undo": None}

        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404


async def test_redo_create_and_undo_it_again():
    async with running_app() as (app, client, adapter):
        signal = await _signal(client, name="Redo me")
        undo_id = (await client.get("/api/library/undo")).json()["undo"]["id"]
        await client.post("/api/library/undo")
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404

        redo = await client.post("/api/library/redo", json={"id": undo_id})
        assert redo.status_code == 200
        assert redo.json()["redone"] == "create Redo me"
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 200
        assert (await client.get("/api/library/undo")).json()["undo"]["id"] == undo_id

        await client.post("/api/library/undo")
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404


async def test_new_library_edit_invalidates_redo():
    async with running_app() as (app, client, adapter):
        signal = await _signal(client, name="First")
        undo_id = (await client.get("/api/library/undo")).json()["undo"]["id"]
        await client.post("/api/library/undo")
        assert (await client.get("/api/library/redo")).json()["redo"]["id"] == undo_id
        await _signal(client, name="Second")
        assert (await client.get("/api/library/redo")).json() == {"redo": None}
        assert (await client.post("/api/library/redo", json={"id": undo_id})).status_code == 409
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404


async def test_redo_replays_multiple_undone_edits_in_order():
    async with running_app() as (app, client, adapter):
        signal = await _signal(client, name="Original")
        create_id = (await client.get("/api/library/undo")).json()["undo"]["id"]
        await client.patch(f"/api/signals/{signal['id']}", json={"station": "Changed"})
        edit_id = (await client.get("/api/library/undo")).json()["undo"]["id"]

        await client.post("/api/library/undo")
        await client.post("/api/library/undo")
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404
        assert (await client.get("/api/library/redo")).json()["redo"]["id"] == create_id
        assert (await client.post("/api/library/redo", json={"id": edit_id})).status_code == 409

        await client.post("/api/library/redo", json={"id": create_id})
        assert (await client.get(f"/api/signals/{signal['id']}")).json()["station"] == ""
        await client.post("/api/library/redo", json={"id": edit_id})
        assert (await client.get(f"/api/signals/{signal['id']}")).json()["station"] == "Changed"


async def test_undo_of_single_patch_restores_prior_fields():
    async with running_app() as (app, client, adapter):
        signal = await _signal(client, name="A", station="Original")
        await client.patch(f"/api/signals/{signal['id']}", json={"station": "Changed"})

        undo = (await client.get("/api/library/undo")).json()["undo"]
        assert undo["label"] == "edit A"

        resp = await client.post("/api/library/undo")
        assert resp.json()["undone"] == "edit A"

        stored = (await client.get(f"/api/signals/{signal['id']}")).json()
        assert stored["station"] == "Original"


async def test_undo_of_bulk_delete_restores_row_tags_and_journal_link():
    async with running_app() as (app, client, adapter):
        tag = await _tag(client, "DX")
        signal = await _signal(client, name="A", tag_ids=[tag["id"]])
        # A journal entry naming this signal (PATCH .../identified writes one).
        await client.patch(f"/api/signals/{signal['id']}", json={"status": "identified"})

        await client.post("/api/library/bulk", json={"ids": [signal["id"]], "op": "delete"})

        # journal_entries has no FK on signal_id, so the link survives even
        # while the signal row itself is gone.
        mid_delete = (await client.get("/api/journal")).json()
        assert any(e["signal_id"] == signal["id"] for e in mid_delete)
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404

        resp = await client.post("/api/library/undo")
        assert resp.json()["undone"] == "delete 1 memories"

        restored = (await client.get(f"/api/signals/{signal['id']}")).json()
        assert restored["name"] == "A"
        assert restored["tag_ids"] == [tag["id"]]

        journal_after = (await client.get("/api/journal")).json()
        matching = [e for e in journal_after if e["signal_id"] == signal["id"]]
        assert matching and all(e["signal_name"] == "A" for e in matching)

        undo_id = (await client.get("/api/library/redo")).json()["redo"]["id"]
        redo = await client.post("/api/library/redo", json={"id": undo_id})
        assert redo.status_code == 200, redo.text
        assert (await client.get(f"/api/signals/{signal['id']}")).status_code == 404


async def test_undo_of_bulk_tag_add():
    async with running_app() as (app, client, adapter):
        tag = await _tag(client, "DX")
        a = await _signal(client, name="A")
        await client.post(
            "/api/library/bulk", json={"ids": [a["id"]], "op": "tag_add", "value": tag["id"]}
        )

        resp = await client.post("/api/library/undo")
        assert resp.json()["undone"] == "tag +DX on 1 memories"

        stored = (await client.get(f"/api/signals/{a['id']}")).json()
        assert stored["tag_ids"] == []
