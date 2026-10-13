"""Plain-language "how to fix" table for errors msdr shows.

Each entry maps error text (as the code really produces it) to why it
happened, numbered steps and up to three action buttons. Pure data plus a
matcher: no web imports, so the API and tests share it.

Patterns are case-insensitive regexes searched anywhere in the text. The
table is ordered and the first match wins, so put specific entries before
general ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from msdr.decoder.registry import DECODERS

ActionKind = Literal["copy_command", "open_url", "copy_diagnostics"]

MAX_ACTIONS = 3
_REPO = "https://github.com/gdamdam/msdr"


@dataclass(frozen=True)
class FixAction:
    kind: ActionKind
    label: str
    value: str = ""  # command text or URL; unused for copy_diagnostics


@dataclass(frozen=True)
class FixIt:
    id: str
    patterns: tuple[str, ...]
    title: str
    why: str
    steps: tuple[str, ...]
    actions: tuple[FixAction, ...] = ()

    def as_api(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "why": self.why,
            "steps": list(self.steps),
            "actions": [
                {"kind": a.kind, "label": a.label, "value": a.value} for a in self.actions
            ],
        }


def _diag() -> FixAction:
    return FixAction("copy_diagnostics", "Copy diagnostics")


def _binary_hint(binary: str) -> str:
    # Reuse the decoder registry's wording so the install advice lives in one place.
    for spec in DECODERS.values():
        if binary in spec.binaries and spec.install_hint:
            return spec.install_hint
    return ""


def _decoder_missing(binary: str, tool_url: str) -> FixIt:
    hint = _binary_hint(binary)
    actions = [FixAction("open_url", f"{binary} home page", tool_url)]
    if hint.startswith("brew install "):
        actions.insert(0, FixAction("copy_command", "Copy install command", hint))
    return FixIt(
        id=f"decoder-missing-{binary}",
        patterns=(rf"required binary '{re.escape(binary)}' not found",),
        title=f"{binary} is not installed",
        why=f"This decoder runs the external program {binary}, and msdr could not find it.",
        steps=(
            hint or f"Install {binary}.",
            "Make sure it is on your PATH or in ~/.local/bin.",
            "Restart msdr, then pick the decoder again.",
        ),
        actions=tuple(actions),
    )


FIXITS: tuple[FixIt, ...] = (
    _decoder_missing("multimon-ng", "https://github.com/EliasOenal/multimon-ng"),
    _decoder_missing("dsd-fme", "https://github.com/lwvmobile/dsd-fme"),
    _decoder_missing("direwolf", "https://github.com/wb2osz/direwolf"),
    _decoder_missing("minimodem", "https://github.com/kamalmostafa/minimodem"),
    _decoder_missing("jt9", "https://wsjt.sourceforge.io/wsjtx.html"),
    _decoder_missing("rtl_433", "https://github.com/merbanan/rtl_433"),
    _decoder_missing("freedv_rx", "https://github.com/drowe67/codec2"),
    *(
        _decoder_missing(name, "https://github.com/projecthorus/radiosonde_auto_rx")
        for name in ("dfm09mod", "m10mod", "m20mod", "rs92mod")
    ),
    FixIt(
        id="sdrconnect-websocket-off",
        patterns=(r"preferences\s*\S*\s*websocket server",),
        title="SDRconnect's WebSocket server is off",
        why="msdr controls the radio through SDRconnect's WebSocket server, which is switched "
        "off by default.",
        steps=(
            'In SDRconnect, open the Primary SP "..." menu and choose Preferences.',
            "Open the WebSocket Server section and enable it (default port 5454).",
            "Come back to msdr and press Retry.",
        ),
        actions=(_diag(),),
    ),
    FixIt(
        id="sdrconnect-version",
        patterns=(r"sdrconnect.{0,40}(api )?version", r"unsupported (sdrconnect|api)"),
        title="SDRconnect version not supported",
        why="msdr speaks a specific version of SDRconnect's WebSocket API and this "
        "version answered differently.",
        steps=(
            "Update SDRconnect to the current release from SDRplay.",
            "Restart SDRconnect and msdr.",
            "If it still fails, copy the diagnostics and include them in a bug report.",
        ),
        actions=(
            FixAction("open_url", "SDRplay downloads", "https://www.sdrplay.com/downloads/"),
            _diag(),
        ),
    ),
    FixIt(
        id="sdrconnect-unreachable",
        patterns=(
            r"sdrconnect not connected",
            r"connection refused",
            r"not connected",
            r"connection closed",
            r"^disconnected$",
        ),
        title="msdr cannot reach SDRconnect",
        why="Nothing is answering on SDRconnect's WebSocket port (5454), so SDRconnect is "
        "closed, still starting, or its WebSocket server is off.",
        steps=(
            "Start SDRconnect and wait for it to finish loading.",
            'Check SDRconnect "..." > Preferences > WebSocket Server is enabled on port 5454.',
            "If you changed the port, start msdr with --port to match.",
            "Press Retry in msdr.",
        ),
        actions=(_diag(),),
    ),
    FixIt(
        id="receiver-stopped",
        patterns=(r"receiver stopped", r"press play in sdrconnect"),
        title="The receiver is stopped",
        why="SDRconnect is connected but its receiver is not running, so there is no signal "
        "to work with.",
        steps=("Press Play in SDRconnect.", "Try the action again."),
    ),
    FixIt(
        id="disk-full",
        patterns=(r"low disk", r"no space left", r"disk full", r"errno 28"),
        title="Not enough disk space to record",
        why="IQ recordings are large, and the recorder refuses to start when free space is "
        "below its safety margin.",
        steps=(
            "Delete old recordings or move them to another drive.",
            "Or point the recorder at a drive with more space in Setup.",
            "Lower the minimum free space only if you know the drive can cope.",
        ),
    ),
    FixIt(
        id="port-in-use",
        patterns=(r"address already in use", r"errno (48|98)\b", r"only one usage of each socket"),
        title="msdr's web port is already in use",
        why="Another program, often an earlier msdr still running, holds port 8765.",
        steps=(
            "Close the other msdr window or process.",
            "Or start msdr on a free port: msdr --web-port 8766",
        ),
        actions=(FixAction("copy_command", "Copy command", "msdr --web-port 8766"),),
    ),
    FixIt(
        id="wifi-helper-missing",
        patterns=(
            r"^helper not found",
            r"helper's python venv is broken",
            r"cannot read helper",
            r"failed to start helper",
            r"msdr-wifi-capture",
        ),
        title="Wi-Fi capture helper missing or not runnable",
        why="Wi-Fi capture runs through the separate msdr-wifi-capture helper, which msdr "
        "could not find or start (it may be missing, moved, or not executable).",
        steps=(
            "Install libusb: brew install libusb",
            "Create the helper as described in the Wi-Fi guide, and run "
            "msdr-wifi-capture --list to check it.",
            "If the helper folder was moved, rebuild its virtualenv.",
            "Set the helper path in Setup > Wi-Fi if it is somewhere unusual.",
        ),
        actions=(
            FixAction("copy_command", "Copy libusb command", "brew install libusb"),
            FixAction("open_url", "Wi-Fi setup guide", f"{_REPO}/blob/main/docs/wifi.md"),
            _diag(),
        ),
    ),
    FixIt(
        id="ble-dependency-missing",
        patterns=(
            r"pyusb or libusb is not available",
            r"pyserial is not available",
            r"nobackenderror",
        ),
        title="Bluetooth capture libraries missing",
        why="BLE capture needs the optional USB (pyusb and libusb) or serial (pyserial) "
        "libraries, which are not installed.",
        steps=(
            "Install libusb: brew install libusb",
            "Install the optional BLE extras in msdr's environment: pip install 'msdr[ble]'",
            "Restart msdr.",
        ),
        actions=(
            FixAction("copy_command", "Copy libusb command", "brew install libusb"),
            FixAction("copy_command", "Copy pip command", "pip install 'msdr[ble]'"),
            FixAction("open_url", "BLE setup guide", f"{_REPO}/blob/main/docs/ble.md"),
        ),
    ),
    FixIt(
        id="ble-dongle-access",
        patterns=(
            r"usb device .* not found",
            r"bring-up failed",
            r"could not open ",
            r"serial port .* not found",
            r"no response from ",
            r"does not answer as sniffle",
            r"handshake failed",
            r"access denied|insufficient permissions|resource busy|\[errno 13\]",
        ),
        title="Bluetooth dongle cannot be opened",
        why="msdr could not open the dongle: it is unplugged, claimed by macOS, in use by "
        "another program, or (for Sniffle) not running Sniffle firmware.",
        steps=(
            "Plug the dongle in directly and check it appears in the detected-devices list.",
            "Use an external dongle, not the Mac's built-in Bluetooth.",
            "Close any other program using the dongle, then rescan.",
            "For a SONOFF dongle, make sure it is flashed with Sniffle firmware.",
        ),
        actions=(
            FixAction("open_url", "BLE setup guide", f"{_REPO}/blob/main/docs/ble.md"),
            _diag(),
        ),
    ),
    FixIt(
        id="nextpass-missing",
        patterns=(r"nextpass not installed", r"bad nextpass output"),
        title="Satellite pass tool (nextpass) missing",
        why="Pass predictions use the nextpass program, which msdr could not run.",
        steps=(
            "Install nextpass: pip install git+https://github.com/gdamdam/nextpass",
            "Or set its full path in Setup if it is installed somewhere else.",
            "Refresh the passes list.",
        ),
        actions=(
            FixAction(
                "copy_command",
                "Copy install command",
                "pip install git+https://github.com/gdamdam/nextpass",
            ),
            FixAction("open_url", "nextpass on GitHub", "https://github.com/gdamdam/nextpass"),
        ),
    ),
)

_COMPILED: tuple[tuple[re.Pattern[str], FixIt], ...] = tuple(
    (re.compile(p, re.IGNORECASE), f) for f in FIXITS for p in f.patterns
)


def match(text: str | None) -> FixIt | None:
    """The first table entry (in table order) whose pattern is found in `text`."""
    if not text:
        return None
    for pat, fix in _COMPILED:
        if pat.search(text):
            return fix
    return None


def format_diagnostics(info: dict[str, str]) -> str:
    """Render a short plain-text block for pasting into a bug report."""
    return "\n".join(f"{k}: {v}" for k, v in info.items())
