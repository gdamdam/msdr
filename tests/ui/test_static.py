import contextlib
import re
from pathlib import Path

import httpx

from msdr.api.app import create_app
from msdr.engine.fake_adapter import FakeAdapter
from msdr.settings import Settings
from msdr.storage.db import Database

UI_DIR = Path(__file__).resolve().parent.parent.parent / "msdr" / "ui"

_IMPORT_RE = re.compile(r"""(?:from|import)\s+["'](\.\.?/[^"']+)["']""")


@contextlib.asynccontextmanager
async def running_app():
    adapter = FakeAdapter()
    settings = Settings(dwell_seconds=0.05, db_path=":memory:")
    db = Database(":memory:")
    app = create_app(settings, adapter=adapter, db=db)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_index_serves_msdr_app():
    async with running_app() as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "<msdr-app>" in resp.text


async def test_app_js_is_javascript():
    async with running_app() as client:
        resp = await client.get("/app.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]


def _relative_imports(path: Path) -> list[str]:
    return _IMPORT_RE.findall(path.read_text())


def test_relative_imports_resolve_to_existing_files():
    js_files = [UI_DIR / "app.js", *sorted((UI_DIR / "components").glob("*.js"))]
    assert js_files, "expected at least app.js and one component"

    missing = []
    for js_file in js_files:
        for spec in _relative_imports(js_file):
            resolved = (js_file.parent / spec.split("?", 1)[0]).resolve()
            if not resolved.is_file():
                missing.append(f"{js_file.name}: {spec} -> {resolved}")
    assert not missing, "unresolved relative imports:\n" + "\n".join(missing)


async def test_ui_files_revalidate_but_api_does_not():
    # A stale tab after an upgrade must never keep old JS: UI responses are
    # no-cache (ETag revalidation), API responses are left alone.
    async with running_app() as client:
        page = await client.get("/")
        assert page.headers.get("cache-control") == "no-cache"
        js = await client.get("/app.js")
        assert js.headers.get("cache-control") == "no-cache"
        for path in ("/api.js", "/api.js?v=0.62.2"):
            helper = await client.get(path)
            assert helper.status_code == 200
            assert helper.headers.get("cache-control") == "no-cache"
        api = await client.get("/api/state")
        assert api.headers.get("cache-control") is None
