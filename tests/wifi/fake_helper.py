#!/usr/bin/env python3
"""Fake stand-in for `msdr-wifi-capture` (docs/wifi.md's "Helper command
line"), used only by tests/wifi and tests/api -- invoked as
`[sys.executable, __file__]` via `settings.wifi.helper_command` (never a real
adapter or driver). Mirrors the real helper's argv, stdout (classic pcap) and
stderr (JSON-lines events) closely enough to exercise WifiSupervisor end to
end.

Mode selected by the `MSDR_WIFI_FAKE_MODE` env var:
  - "normal" (default): ready, then beacon frames forever until SIGTERM/SIGINT.
  - "no_device": exits 1 with an "error" event (code "open_failed") before
    any "ready" -- docs/wifi.md's documented "no adapter" exit code.
  - "crash": like normal, but calls os._exit(3) (simulated USB error) after
    `MSDR_WIFI_FAKE_CRASH_AFTER` frames (default 5).
  - "malformed": like normal, but every 5th record's radiotap payload has a
    bad version byte -- a well-formed *pcap* record (so PcapStreamReader
    stays in sync) whose radiotap parse fails, exercising the
    "malformed stream counted without crashing" path end to end.

`--list` prints one JSON device line (docs/wifi.md's example payload) and
exits 0, or -- with `MSDR_WIFI_FAKE_LIST_EMPTY=1` -- prints nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

from msdr.wifi.pcap import write_header, write_record  # noqa: E402

_STOP = False


def _on_signal(signum, frame) -> None:
    global _STOP
    _STOP = True


def _emit_stderr(obj: dict) -> None:
    sys.stderr.write(json.dumps(obj) + "\n")
    sys.stderr.flush()


def _frequency_mhz(channel: int) -> int:
    return 2407 + 5 * channel if channel <= 14 else 5000 + 5 * channel


def run_list() -> None:
    if os.environ.get("MSDR_WIFI_FAKE_LIST_EMPTY") == "1":
        sys.exit(0)
    print(
        json.dumps(
            {
                "vid": 3034,
                "pid": 34834,
                "bus": 1,
                "address": 5,
                "product": "802.11n NIC",
                "serial": "123456",
                "supported": True,
            }
        )
    )
    sys.exit(0)


def run_capture(channel: int) -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    mode = os.environ.get("MSDR_WIFI_FAKE_MODE", "normal")
    delay = float(os.environ.get("MSDR_WIFI_FAKE_DELAY_S", "0.02"))
    crash_after = int(os.environ.get("MSDR_WIFI_FAKE_CRASH_AFTER", "5"))

    if mode == "no_device":
        _emit_stderr({"event": "error", "code": "open_failed", "message": "no adapter found"})
        sys.exit(1)

    frequency_mhz = _frequency_mhz(channel)
    _emit_stderr(
        {
            "event": "ready",
            "channel": channel,
            "frequency_mhz": frequency_mhz,
            "helper_version": "fake-0.0.0",
            "upstream_commit": "347e9f172750e0c0b7c45b6dcfef7e780d70d144",
            "host_tx_dma_disabled": True,
            "mac_tx_enabled": False,
            "reg_cr_before": "0x00000003",
            "reg_cr_after": "0x00000000",
        }
    )

    stdout = sys.stdout.buffer
    stdout.write(write_header())
    stdout.flush()

    frame_index = 0
    while not _STOP:
        frame_index += 1
        bssid = f"aa:bb:cc:dd:ee:{(frame_index % 250):02x}"
        if mode == "malformed" and frame_index % 5 == 0:
            radiotap = b"\x99\x00\x08\x00\x00\x00\x00\x00"  # bad version byte
            payload = radiotap
        else:
            radiotap = fixtures.build_radiotap(channel_mhz=frequency_mhz, flags=0x10)
            frame = fixtures.append_fcs(
                fixtures.build_beacon_frame(bssid=bssid, ds_channel=channel)
            )
            payload = radiotap + frame
        stdout.write(write_record(time.time(), payload))
        stdout.flush()

        if frame_index % 50 == 0:
            _emit_stderr(
                {
                    "event": "stats",
                    "frames": frame_index,
                    "bad_fcs": 0,
                    "usb_errors": 0,
                    "elapsed_s": frame_index * delay,
                }
            )
        if mode == "crash" and frame_index >= crash_after:
            os._exit(3)
        time.sleep(delay)

    _emit_stderr({"event": "stopped", "reason": "signal"})
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--max-seconds", type=float)
    args = parser.parse_args()
    if args.list:
        run_list()
    else:
        run_capture(args.channel)


if __name__ == "__main__":
    main()
