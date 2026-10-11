"""Fixtures for the browser tests in tests/e2e (see docs/superpowers/specs/
2026-09-23-sp1-trust-feedback-design.md, section C).

These drive a real Chromium against a real msdr app (FastAPI + uvicorn) wired
to a FakeSDRconnect, so they need `--e2e` to run at all (see
pytest_collection_modifyitems below) and a Chromium that can actually launch.
Chromium can't launch inside this repo's macOS Seatbelt sandbox (a mach port
permission error) -- run unsandboxed, or let a human run them.
"""

import asyncio
import os
import threading
from pathlib import Path

import pytest
import uvicorn

from msdr.api.app import create_app
from msdr.radio.fake_server import FakeSDRconnect
from msdr.settings import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
# pytest-playwright/playwright launch a Node driver subprocess lazily (on
# first use of the `playwright` fixture), which is after collection -- so
# setting this here, before any fixture runs, is early enough.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_REPO_ROOT / ".playwright"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="run the tests/e2e browser tests (need a real Chromium)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Tests are still collected either way (--collect-only --e2e must show
    # them); without --e2e they're skipped at run time rather than deselected.
    if config.getoption("--e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="needs --e2e (real Chromium; see tests/e2e/conftest.py)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    # Only the headless-shell Chromium build is installed under
    # PLAYWRIGHT_BROWSERS_PATH here -- pick that channel by default so a bare
    # `--e2e` run works without also having to pass --browser-channel. An
    # explicit --browser-channel (e.g. `chrome` for the system install) wins,
    # for machines where the headless-shell download isn't available.
    return {"channel": "chromium-headless-shell", **browser_type_launch_args}


class LiveApp:
    """A real msdr FastAPI app + FakeSDRconnect, both driven from one asyncio
    event loop in a background thread and served over real HTTP by uvicorn,
    so an actual browser can talk to them."""

    def __init__(self, base_url: str, app, fake: FakeSDRconnect, loop: asyncio.AbstractEventLoop):
        self.base_url = base_url
        self.app = app
        self.fake = fake
        self._loop = loop

    @property
    def service(self):
        return self.app.state.service

    def run(self, coro, timeout: float = 5.0):
        """Run a coroutine on the app's own event loop from the test thread
        (e.g. fake.wait_for_set, fake.stop/start -- FakeSDRconnect isn't
        thread-safe, it must only ever be touched from its own loop)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def drop_echo(self, prop: str) -> None:
        """Make the fake accept a set_property for `prop` without echoing
        property_changed, and leave a get_property for it unanswered too --
        the restore falls back to reading the value back after a missing
        echo, so both must be dropped for the step to fail. Scheduled on the
        fake's own loop so this stays thread-safe like everything else that
        touches it."""
        self._loop.call_soon_threadsafe(self.fake.drop_echo.add, prop)
        self._loop.call_soon_threadsafe(self.fake.drop_get.add, prop)


@pytest.fixture
def live_app(tmp_path: Path):
    loop = asyncio.new_event_loop()
    box: dict = {}
    ready = threading.Event()
    startup_error: dict = {}

    async def _main() -> None:
        fake = FakeSDRconnect(port=0)
        await fake.start()
        settings = Settings(
            host="127.0.0.1",
            port=fake.port,
            web_port=0,
            db_path=tmp_path / "m.db",
            # Never run the developer's real `nextpass` against their real
            # location: real passes made satellite defaults depend on the time
            # of day (the SKY default prefers the soonest pass).
            nextpass_bin=str(tmp_path / "no-nextpass"),
            location_file=str(tmp_path / "no-location.json"),
            tle_cache_dir=str(tmp_path / "tle"),
            nextpass_cache_dir=str(tmp_path / "nextpass"),
        )
        app = create_app(settings, settings_path=tmp_path / "s.json")
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = uvicorn.Server(config)
        serve_task = asyncio.ensure_future(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        web_port = server.servers[0].sockets[0].getsockname()[1]
        box.update(fake=fake, app=app, server=server, base_url=f"http://127.0.0.1:{web_port}")
        ready.set()
        await serve_task
        await fake.stop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main())
        except BaseException as exc:  # noqa: BLE001 -- surfaced on the test thread below
            startup_error["exc"] = exc
            ready.set()
        finally:
            loop.close()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    if not ready.wait(timeout=10):
        raise RuntimeError("live_app server did not start within 10s")
    if "exc" in startup_error:
        raise startup_error["exc"]

    live = LiveApp(box["base_url"], box["app"], box["fake"], loop)
    yield live

    box["server"].should_exit = True
    thread.join(timeout=10)
