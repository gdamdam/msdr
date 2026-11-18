"""Query and body values that used to reach the engines unchecked."""

from tests.api.test_app import running_app


async def test_passes_overrides_are_bounded_like_the_settings():
    async with running_app() as (_app, client, _adapter):
        for query in ("days=100000", "days=0", "top=0", "min_elevation=nan", "horizon=90"):
            response = await client.get(f"/api/passes?{query}")
            assert response.status_code == 422, query
        assert (await client.post("/api/passes/refresh?days=-1")).status_code == 422


async def test_find_status_must_be_a_known_value():
    async with running_app() as (_app, client, _adapter):
        response = await client.patch("/api/finds/any", json={"status": "Ignored "})
        assert response.status_code == 422


async def test_observations_limit_is_clamped():
    async with running_app() as (app, client, _adapter):
        seen = {}
        original = app.state.service.observations.list

        def spy(**kwargs):
            seen.update(kwargs)
            return original(**kwargs)

        app.state.service.observations.list = spy
        response = await client.get("/api/observations?limit=-1&tolerance_hz=-5")
        assert response.status_code == 200
        assert seen["limit"] == 1 and seen["tolerance_hz"] == 0
