#!/usr/bin/env python3
"""M0 probe: talk to a running SDRconnect over its WebSocket API and answer the
open questions in docs/superpowers/specs/2026-09-22-msdr-design.md §16.

Standalone by design: stdlib + `websockets` only, no msdr imports. This has to
run before msdr's own adapter is trusted, so it can't depend on it. The
property name list below is copied from msdr/radio/registry.py -- keep it in
sync by hand if the registry changes before M0 is done.

Usage:
    python scripts/probe_sdrconnect.py [--host HOST] [--port PORT] [--skip N]

Each experiment prompts before running so you can watch SDRconnect's UI react.
Every frame sent/received is timestamped and written to stdout and probe.log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
from pathlib import Path

from websockets.asyncio.client import connect

# Copied from msdr/radio/registry.py property names (wire names only; no msdr import).
PROPERTY_NAMES = [
    "active_antenna",
    "device_sample_rate",
    "device_center_frequency",
    "device_vfo_frequency",
    "demodulator",
    "filter_bandwidth",
    "lna_state",
    "squelch_enable",
    "squelch_threshold",
    "agc_enable",
    "agc_threshold",
    "noise_reduction_enable",
    "noise_reduction_strength",
    "signal_power",
    "signal_snr",
    "overload",
    "can_control",
    "started",
    "api_version",
    "valid_antennas",
    "demod_max_bandwidth",
    "lna_state_min",
    "lna_state_max",
]

LOG_PATH = Path("probe.log")


class FrameLog:
    """Writes every frame to stdout and probe.log with a timestamp, as decided."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")

    def write(self, direction: str, payload: str) -> None:
        ts = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d}"
        line = f"[{ts}] {direction} {payload}"
        print(line)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class Probe:
    """Thin request/response wrapper: sends set_property/get_property, collects
    property_changed pushes in the background so experiments can inspect them.
    """

    def __init__(self, ws, log: FrameLog) -> None:
        self._ws = ws
        self._log = log
        self._events: list[dict] = []
        self._reader_task: asyncio.Task | None = None
        self.reader_error: str | None = None

    def start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop_reader(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                self._log.write("<-", raw)
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._events.append({**msg, "_t": time.monotonic()})
        except Exception as exc:
            # Diagnostic catch-all: any failure here (dropped connection, protocol
            # error) should surface to experiments as a clear "reader stopped"
            # message instead of them silently timing out forever.
            self.reader_error = str(exc)
            self._log.write("!!", f"reader stopped: {exc}")

    async def send(self, event_type: str, property_: str, value: object | None = None) -> None:
        msg = {"event_type": event_type, "property": property_}
        if value is not None:
            msg["value"] = str(value)
        raw = json.dumps(msg)
        self._log.write("->", raw)
        await self._ws.send(raw)

    async def set_property(self, property_: str, value: object) -> None:
        await self.send("set_property", property_, value)

    async def get_property(self, property_: str) -> None:
        await self.send("get_property", property_)

    def events_since(self, t0: float, property_: str | None = None) -> list[dict]:
        return [
            e
            for e in self._events
            if e["_t"] >= t0 and (property_ is None or e.get("property") == property_)
        ]


async def wait_for_property(probe: Probe, name: str, timeout: float, since: float) -> dict | None:
    """Poll for the first event for `name` arriving at/after `since` (a
    time.monotonic() value taken right before the request that should trigger it).
    Filtering by `since` matters: `_pending` keeps old responses around, and
    without a time floor this would return a stale answer to an earlier request.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe.reader_error is not None:
            print(f"reader stopped: {probe.reader_error}")
            return None
        matches = probe.events_since(since, name)
        if matches:
            return matches[-1]
        await asyncio.sleep(0.05)
    return None


def ask(prompt: str) -> str:
    # Deliberately blocking: this is an interactive probe, not a service, and the
    # asyncio event loop has nothing else to do while waiting on the operator.
    return input(prompt)


def confirm(n: int, skip_below: int, label: str) -> bool:
    if n < skip_below:
        print(f"-- skipping experiment {n} ({label}) per --skip")
        return False
    reply = ask(f"Press Enter to run experiment {n} ({label}) (or s to skip): ")
    return reply.strip().lower() != "s"


async def experiment_1_property_types(probe: Probe) -> None:
    """Q3, Q7: get every registry property, print its raw JSON value type."""
    for name in PROPERTY_NAMES:
        t0 = time.monotonic()
        await probe.get_property(name)
        resp = await wait_for_property(probe, name, timeout=1.5, since=t0)
        if resp is None:
            # get_property_response also carries `property`; wait_for_property
            # doesn't care which event_type, only that the property matches.
            print(f"{name}: no response within timeout")
            continue
        value = resp.get("value")
        elapsed = time.monotonic() - t0
        print(f"{name}: value={value!r} json_type={type(value).__name__} elapsed={elapsed:.3f}s")


async def experiment_2_vfo_outside_span(probe: Probe, orig: dict) -> None:
    """Q1: push the vfo 1 MHz past the *real* span (center +/- sample_rate/2), not
    just past the center -- above 2 MSPS the span is wider than 1 MHz, so a naive
    center+1MHz offset can land back inside the span and silently answer Q1 wrong.
    """
    center = int(orig["device_center_frequency"])
    sample_rate = int(orig["device_sample_rate"])
    half_span = sample_rate // 2
    span_low, span_high = center - half_span, center + half_span
    new_vfo = span_high + 1_000_000
    print(f"span: [{span_low}, {span_high}] (center={center}, sample_rate={sample_rate})")
    print(f"target vfo (outside span): {new_vfo}")
    t0 = time.monotonic()
    await probe.set_property("device_vfo_frequency", new_vfo)
    center_ev = await wait_for_property(probe, "device_center_frequency", timeout=3.0, since=t0)
    vfo_ev = await wait_for_property(probe, "device_vfo_frequency", timeout=3.0, since=t0)
    print(f"center before={center} after={center_ev}")
    print(f"vfo set_to={new_vfo} confirmed={vfo_ev}")
    print(f"elapsed={time.monotonic() - t0:.3f}s")


async def experiment_3_unchanged_echo(probe: Probe, orig: dict) -> None:
    """Q5: set vfo to its current value, see whether property_changed still arrives."""
    vfo = orig["device_vfo_frequency"]
    t0 = time.monotonic()
    await probe.set_property("device_vfo_frequency", vfo)
    ev = await wait_for_property(probe, "device_vfo_frequency", timeout=2.0, since=t0)
    print(f"set unchanged value={vfo}: echo={'yes' if ev else 'no'} event={ev}")
    print(f"elapsed={time.monotonic() - t0:.3f}s")


async def experiment_4_sample_rate_latency(probe: Probe, orig: dict) -> None:
    """Q4: latency of sample_rate change, and whether lna_state/active_antenna move."""
    current = orig["device_sample_rate"]
    candidate = ask(
        f"Current device_sample_rate={current}. Enter a different valid sample rate to try: "
    )
    lna_before = orig.get("lna_state")
    antenna_before = orig.get("active_antenna")
    t0 = time.monotonic()
    await probe.set_property("device_sample_rate", candidate)
    ev = await wait_for_property(probe, "device_sample_rate", timeout=5.0, since=t0)
    elapsed = time.monotonic() - t0
    lna_ev = probe.events_since(t0, "lna_state")
    ant_ev = probe.events_since(t0, "active_antenna")
    print(f"sample_rate change confirmed after {elapsed:.3f}s: {ev}")
    print(f"lna_state disturbed: {bool(lna_ev)} (before={lna_before}, events={lna_ev})")
    print(f"active_antenna disturbed: {bool(ant_ev)} (before={antenna_before}, events={ant_ev})")
    # restore immediately, this experiment intentionally leaves the rate changed otherwise
    t0 = time.monotonic()
    await probe.set_property("device_sample_rate", current)
    ev = await wait_for_property(probe, "device_sample_rate", timeout=5.0, since=t0)
    print(f"restored sample_rate={current} after {time.monotonic() - t0:.3f}s: {ev}")


async def experiment_5_apply_profile(probe: Probe) -> None:
    """Q2: apply_device_profile, list every property_changed within 3s."""
    name = ask("Enter a native SDRconnect profile name to apply: ")
    t0 = time.monotonic()
    await probe.send("apply_device_profile", "device_profile", name)
    await asyncio.sleep(3.0)
    changed = [e for e in probe.events_since(t0) if e.get("event_type") == "property_changed"]
    print(f"apply_device_profile({name!r}): {len(changed)} property_changed events in 3s:")
    for e in changed:
        print(f"  {e.get('property')}={e.get('value')!r}")


async def experiment_6_manual_tuning(probe: Probe) -> None:
    """Q6: user turns the tuning wheel for 5s; count events, report inter-arrival stats."""
    ask("Press Enter, then turn the tuning wheel in SDRconnect for about 5 seconds...")
    t0 = time.monotonic()
    await asyncio.sleep(5.0)
    events = [
        e
        for e in probe.events_since(t0)
        if e.get("event_type") == "property_changed" and e.get("property") == "device_vfo_frequency"
    ]
    print(f"device_vfo_frequency property_changed events in 5s: {len(events)}")
    if len(events) >= 2:
        gaps = [b["_t"] - a["_t"] for a, b in zip(events, events[1:], strict=False)]
        avg = sum(gaps) / len(gaps)
        median = statistics.median(gaps)
        print(
            f"inter-arrival min={min(gaps):.3f}s max={max(gaps):.3f}s "
            f"avg={avg:.3f}s median={median:.3f}s"
        )


async def snapshot(probe: Probe, names: list[str]) -> dict:
    """get_property for `names`, return {name: value} for restore-at-exit."""
    values: dict[str, str] = {}
    for name in names:
        t0 = time.monotonic()
        await probe.get_property(name)
        ev = await wait_for_property(probe, name, timeout=2.0, since=t0)
        if ev is not None:
            values[name] = ev.get("value")
    return values


async def run(args: argparse.Namespace) -> None:
    log = FrameLog(LOG_PATH)
    uri = f"ws://{args.host}:{args.port}/"
    try:
        ws = await connect(uri)
    except OSError as exc:
        print(
            f"Could not connect to {uri}: {exc}\n"
            "Enable it in SDRconnect: Primary SP \"...\" menu -> Preferences -> "
            "WebSocket Server (default port 5454).",
            file=sys.stderr,
        )
        sys.exit(1)

    probe = Probe(ws, log)
    probe.start_reader()
    orig: dict = {}
    try:
        print("Connected. Taking a snapshot of vfo/center/sample_rate for restore-at-exit...")
        orig = await snapshot(
            probe, ["device_vfo_frequency", "device_center_frequency", "device_sample_rate"]
        )
        print(f"snapshot: {orig}")

        if confirm(1, args.skip, "property types (Q3, Q7)"):
            await experiment_1_property_types(probe)
        if confirm(2, args.skip, "vfo outside span (Q1)"):
            await experiment_2_vfo_outside_span(probe, orig)
        if confirm(3, args.skip, "unchanged value echo (Q5)"):
            await experiment_3_unchanged_echo(probe, orig)
        if confirm(4, args.skip, "sample rate latency (Q4)"):
            await experiment_4_sample_rate_latency(probe, orig)
        if confirm(5, args.skip, "apply_device_profile (Q2)"):
            await experiment_5_apply_profile(probe)
        if confirm(6, args.skip, "manual tuning wheel (Q6)"):
            await experiment_6_manual_tuning(probe)
    finally:
        print("Restoring original vfo/center/sample_rate...")
        for name in ("device_sample_rate", "device_center_frequency", "device_vfo_frequency"):
            if name in orig:
                t0 = time.monotonic()
                await probe.set_property(name, orig[name])
                await wait_for_property(probe, name, timeout=5.0, since=t0)
        await probe.stop_reader()
        await ws.close()
        log.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="SDRconnect host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5454, help="SDRconnect port (default: 5454)")
    parser.add_argument("--skip", type=int, default=1, metavar="N", help="skip experiments below N")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")


if __name__ == "__main__":
    main()
