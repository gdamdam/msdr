"""Builds contracts/readiness.py's three-fact records for a decoder: is the
backend here (Installation), has it been shown to work (Validation, from
capability_matrix.py), what is it doing right now (Session, from manager/
band status).

Version probes are best-effort, bounded (2 s) subprocess calls, run only
when asked (never at import time) and cached per resolved binary path so a
repeated describe()/status() call doesn't re-spawn a process.
"""

import logging
import plistlib
import re
import subprocess
from pathlib import Path

import msdr
from msdr.contracts.readiness import DecoderReadiness, Installation, Session, Validation
from msdr.decoder.capability_matrix import MATRIX
from msdr.decoder.registry import DecoderSpec, compatibility_issue

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 2.0

# Cache of binary path -> probed version (or None), so repeated readiness_for
# calls (e.g. one per /api/decoders poll) don't re-spawn a subprocess.
_version_cache: dict[str, str | None] = {}

_MULTIMON_VERSION_RE = re.compile(r"^multimon-ng\s+(\S+)")
_MINIMODEM_VERSION_RE = re.compile(r"^minimodem\s+(\S+)")
# direwolf prints two version strings in "-h" output: "Dire Wolf Release
# X.Y.Z, <month> <year>" (full patch version) and a separate "Dire Wolf
# version X.Y" a few lines later (no patch) -- the Release line is preferred
# (measured on the real /opt/homebrew/bin/direwolf 1.8.1: "Dire Wolf Release
# 1.8.1, November 2025").
_DIREWOLF_VERSION_RE = re.compile(r"Dire Wolf Release\s+([\d.]+)")


def _run(binary: str, args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        # Backends here exit non-zero for "-h"/help (multimon-ng -h -> 2,
        # direwolf -h -> 1) -- checked against the real binaries, not
        # guessed -- so returncode is never used as a success gate below.
        return subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("version probe failed for %s: %s", binary, exc)
        return None


def _probe_version(backend: str, binary: str) -> str | None:
    """One bounded, best-effort probe per backend kind. Every regex/command
    here was checked against the real binary on a macOS install (~/.local/bin,
    /opt/homebrew/bin), not guessed."""
    if backend == "standalone_ft8":
        # The official Mac bundle versions jt9 with WSJT-X; bare binaries
        # without this metadata remain unknown rather than guessed.
        info = Path(binary).parent.parent / "Info.plist"
        try:
            metadata = plistlib.loads(info.read_bytes())
            if metadata.get("CFBundleIdentifier") == "org.k1jt.wsjtx":
                value = metadata.get("CFBundleVersion")
                return str(value) if value else None
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
        return None
    if backend == "multimon":
        proc = _run(binary, ["-h"])
        if proc is None:
            return None
        # measured: multimon-ng -h exits 2; version is stderr's first line.
        match = _MULTIMON_VERSION_RE.match(proc.stderr.strip())
        return match.group(1) if match else None
    if backend == "minimodem":
        proc = _run(binary, ["--version"])
        if proc is None:
            return None
        # measured: minimodem --version exits 0; version is stdout's first line.
        match = _MINIMODEM_VERSION_RE.match(proc.stdout.strip())
        return match.group(1) if match else None
    if backend == "direwolf":
        proc = _run(binary, ["-h"])
        if proc is None:
            return None
        # measured: direwolf -h exits 1; "Dire Wolf Release 1.8.1, ..." on stdout.
        match = _DIREWOLF_VERSION_RE.search(proc.stdout)
        return match.group(1) if match else None
    if backend == "dsdfme":
        # measured: dsd-fme -h prints a "Build Version: AW -128-NOTFOUND"
        # banner on stderr -- not a real version string (build metadata that
        # didn't resolve, not a dsd-fme release number). No reliable version
        # is obtainable from the binary, so this is intentionally always
        # None rather than parsing that placeholder.
        return None
    return None


def cached_version(backend: str, binary: str) -> str | None:
    """Per-binary-path cache: same resolved path never re-probes."""
    if binary not in _version_cache:
        _version_cache[binary] = _probe_version(backend, binary)
    return _version_cache[binary]


def installation_for(spec: DecoderSpec, binaries: dict[str, str | None]) -> Installation:
    issue = compatibility_issue(spec)
    if issue:
        return Installation(state="incompatible", backend=spec.backend, install_hint=issue)
    if spec.id == "lora":
        # LoRaBackend is msdr's own native numpy receiver: always built in,
        # no external process/binary. gr-lora_sdr only backs the optional
        # GrLoRaBackend cross-check, reported here as a hint rather than a
        # blocking "missing" installation.
        from msdr.decoder.pydecoders.lora import _GR_CROSSCHECK_HINT, _gr_lora_available

        hint = "" if _gr_lora_available() else _GR_CROSSCHECK_HINT
        return Installation(
            state="builtin",
            backend=spec.backend,
            binary=None,
            version=msdr.__version__,
            install_hint=hint,
        )
    if spec.id == "adsb":
        # NativeADSBBackend is msdr's own native numpy receiver: always
        # built in, no external process/binary. dump1090-fa remains an
        # alternative (standalone_adsb.py); surface that as a hint rather
        # than a blocking "missing" installation.
        return Installation(
            state="builtin",
            backend=spec.backend,
            binary=None,
            version=msdr.__version__,
            install_hint=spec.install_hint,
        )
    if spec.backend in {"python", "standalone_protocol", "standalone_dsc", "standalone_navigation"}:
        # Built-in: versioned with msdr itself, no external process/binary.
        return Installation(
            state="builtin", backend=spec.backend, binary=None, version=msdr.__version__
        )

    # Match build_backend's preferred alternative, so readiness reports the
    # binary and version that will actually decode APRS/packet traffic.
    if spec.alt_backend is not None:
        alt_name, _ = spec.alt_backend
        alt_path = binaries.get(alt_name)
        if alt_path:
            return Installation(
                state="present",
                backend=alt_name,
                binary=alt_path,
                version=cached_version(alt_name, alt_path),
                install_hint=spec.install_hint,
            )

    names = spec.binaries
    resolved = [binaries.get(name) for name in names]
    if not names or not all(resolved):
        return Installation(
            state="missing",
            backend=spec.backend,
            binary=None,
            version=None,
            install_hint=spec.install_hint,
        )
    binary = resolved[0]
    assert binary is not None
    version = cached_version(spec.backend, binary)
    return Installation(
        state="present",
        backend=spec.backend,
        binary=binary,
        version=version,
        install_hint=spec.install_hint,
    )


# Manager channel states (msdr/decoder/channel.py DecodeChannel.state):
# "starting" | "running" | "error" | "stopped". BandChannel states
# (msdr/decoder/band.py): "running" | "error".
_CHANNEL_STATE_TO_SESSION = {
    "starting": "acquiring",
    "stopped": "idle",
    "error": "error",
}


def session_from_facts(
    decoder_id: str, facts: dict, *, source_id: str | None = None, detail: str = ""
) -> Session:
    """One decoder's Session from its own facts (S4 §1) -- never from a
    manager-wide proxy. `facts` keys (all optional): state, error, events,
    valid_frames, synchronized, quiet, unsupported (reason text).

    Precedence: error > valid_frames > unsupported_input > synchronized >
    unverified_output > quiet > acquiring. valid_frames outranks the rest
    because an integrity-checked frame is the strongest evidence the input
    is right; "unverified_output" covers every policy that produced events
    without a passed check (none_exposed, or fec/parity that did not pass).
    """
    state = facts.get("state")
    events = int(facts.get("events") or 0)
    valid = int(facts.get("valid_frames") or 0)
    common = {"events": events, "valid_frames": valid, "source_id": source_id}
    if state == "error" or (facts.get("error") and state not in ("running", "starting")):
        return Session(state="error", detail=facts.get("error") or "", **common)
    unsupported = facts.get("unsupported")
    if state in ("starting", "stopped") and not unsupported:
        return Session(state=_CHANNEL_STATE_TO_SESSION[state], detail=detail, **common)
    if state not in ("running", "unsupported_input") and not unsupported:
        return Session(state="idle", detail=detail, **common)
    if valid > 0:
        return Session(state="valid_frames", detail=detail, **common)
    if unsupported or state == "unsupported_input":
        return Session(state="unsupported_input", detail=str(unsupported or detail), **common)
    if facts.get("synchronized") is True:
        return Session(state="synchronized", detail=detail, **common)
    if events > 0:
        return Session(state="unverified_output", detail=detail, **common)
    if facts.get("quiet"):
        return Session(state="quiet", detail=detail, **common)
    return Session(state="acquiring", detail=detail, **common)


def decoder_facts(
    decoder_id: str,
    *,
    state: str,
    error: str | None = None,
    events: int = 0,
    valid_frames: int = 0,
    last_event_ts: float | None = None,
    synchronized: bool | None = None,
    quiet: bool = False,
    unsupported: str | None = None,
) -> dict:
    """The per-decoder status dict manager/band/replay status() carry (S4 §1):
    the legacy id/state/error keys plus the session facts and their
    session_state."""
    facts = {
        "id": decoder_id,
        "state": state,
        "error": error,
        "events": events,
        "valid_frames": valid_frames,
        "last_event_ts": last_event_ts,
        "synchronized": synchronized,
    }
    if unsupported:
        facts["unsupported"] = unsupported
    session = session_from_facts(decoder_id, {**facts, "quiet": quiet})
    facts["session_state"] = session.state
    return facts


def merge_band_facts(decoder_id: str, band_channels: list[dict]) -> dict:
    """Sum one decoder's facts over every band channel running it."""
    events = valid = 0
    synced: bool | None = None
    errors: list[str] = []
    for channel in band_channels:
        per = next(
            (d for d in channel.get("decoder_status") or [] if d.get("id") == decoder_id), None
        )
        if per is None:
            # Pre-S4 band status (no per-decoder facts): channel totals only.
            events += int(channel.get("events") or 0)
            if channel.get("state") == "error":
                errors.append(channel.get("error") or "")
            continue
        events += int(per.get("events") or 0)
        valid += int(per.get("valid_frames") or 0)
        if per.get("synchronized") is True:
            synced = True
        elif per.get("synchronized") is False and synced is None:
            synced = False
        if per.get("state") == "error":
            errors.append(per.get("error") or "")
    all_errored = bool(errors) and len(errors) == len(band_channels)
    return {
        "state": "error" if all_errored else "running",
        "error": errors[0] if all_errored else None,
        "events": events,
        "valid_frames": valid,
        "synchronized": synced,
    }


def session_for(decoder_id: str, manager_status: dict | None) -> Session:
    """Session for one decoder from a status dict: manager.status(), a
    ReplayDecodeSession.status() (same shape), or a single decoder_facts()
    dict."""
    if not manager_status:
        return Session(state="idle")

    if "decoders" not in manager_status and "state" in manager_status:
        return session_from_facts(decoder_id, manager_status)

    source_id = (
        manager_status.get("source_id")
        or manager_status.get("signal_id")
        or manager_status.get("source")
    )
    channel = next(
        (d for d in manager_status.get("decoders", []) if d.get("id") == decoder_id), None
    )
    if channel is None:
        # Also check band status (multi-channel): any BandChannel running
        # this decoder counts, keyed by its own decoder list per channel.
        band = manager_status.get("band") or {}
        band_channels = [
            c for c in band.get("channels", []) if decoder_id in (c.get("decoders") or [])
        ]
        if not band_channels:
            return Session(state="idle", source_id=source_id)
        return session_from_facts(
            decoder_id,
            merge_band_facts(decoder_id, band_channels),
            source_id=source_id,
            detail="band decode",
        )

    if "events" not in channel:
        # Pre-S4 status (no per-decoder facts): the manager-wide
        # events_total is the only signal -- kept so older callers/tests
        # read the same as before.
        channel = {**channel, "events": manager_status.get("events_total", 0)}
    quiet = bool((manager_status.get("audio") or {}).get("silent"))
    return session_from_facts(decoder_id, {**channel, "quiet": quiet}, source_id=source_id)


def readiness_for(
    spec: DecoderSpec, binaries: dict[str, str | None], manager_status: dict | None
) -> DecoderReadiness:
    installation = installation_for(spec, binaries)
    entry = MATRIX.get(spec.id)
    validation = entry.validation if entry is not None else Validation()
    session = session_for(spec.id, manager_status)
    return DecoderReadiness(
        decoder_id=spec.id, installation=installation, validation=validation, session=session
    )
