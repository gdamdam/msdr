"""`msdr` CLI: the per-request access log is only on with --debug."""

from msdr.api.main import _parse_args, _uvicorn_options


def test_access_log_is_off_by_default():
    args = _parse_args([])
    assert args.debug is False
    assert _uvicorn_options(args.debug)["access_log"] is False


def test_debug_flag_turns_access_log_on():
    args = _parse_args(["--debug"])
    assert _uvicorn_options(args.debug) == {"log_level": "info", "access_log": True}
