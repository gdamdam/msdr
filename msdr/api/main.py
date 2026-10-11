"""CLI entry point: `python -m msdr.api.main` / the `msdr` console script."""

import argparse
import logging
import threading
import webbrowser
from pathlib import Path

import uvicorn

from msdr.api.app import create_app
from msdr.settings import Settings

logger = logging.getLogger("msdr")


def _configure_logging() -> None:
    # uvicorn.run() installs its own logging config, which leaves the "msdr"
    # logger (parent of "msdr.api.service") without a handler of its own and
    # its records go nowhere. Give it one directly so the startup URL and the
    # disconnected-SDRconnect hint actually print.
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="msdr", description="msdr: SDRconnect listening companion"
    )
    parser.add_argument("--host", help="SDRconnect WebSocket host (default from settings)")
    parser.add_argument("--port", type=int, help="SDRconnect WebSocket port (default)")
    parser.add_argument("--web-port", type=int, help="msdr's own HTTP port (default from settings)")
    parser.add_argument("--db", help="path to the msdr sqlite database")
    parser.add_argument("--settings", help="path to the msdr settings.json file")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the UI in a browser on start"
    )
    parser.add_argument(
        "--debug", action="store_true", help="log every HTTP request and msdr debug messages"
    )
    return parser.parse_args(argv)


def _uvicorn_options(debug: bool) -> dict:
    # The UI polls several endpoints every second or so; uvicorn's access log
    # turned that into a line per request. Keep it for --debug only.
    return {"log_level": "info", "access_log": debug}


def _open_browser_later(url: str, delay_s: float = 1.0) -> None:
    # uvicorn.run() blocks, so the browser is opened from a timer thread once the
    # server has had a moment to bind; failures (headless, no browser) are ignored.
    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - best effort convenience only
            logger.info("could not open a browser; visit %s", url)

    timer = threading.Timer(delay_s, _open)
    timer.daemon = True  # never hold the process open if uvicorn exits first
    timer.start()


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    args = _parse_args(argv)
    if args.debug:
        logger.setLevel(logging.DEBUG)
    settings_path = Path(args.settings) if args.settings else None
    settings = Settings.load(settings_path)
    if args.host is not None:
        settings.host = args.host
    if args.port is not None:
        settings.port = args.port
    if args.web_port is not None:
        settings.web_port = args.web_port
    if args.db is not None:
        settings.db_path = Path(args.db)

    app = create_app(settings, settings_path=settings_path)
    url = f"http://127.0.0.1:{settings.web_port}"
    logger.info("msdr listening on %s", url)
    if not args.no_browser:
        _open_browser_later(url)
    uvicorn.run(app, host="127.0.0.1", port=settings.web_port, **_uvicorn_options(args.debug))


if __name__ == "__main__":
    main()
