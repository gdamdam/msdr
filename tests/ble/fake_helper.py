"""Stand-in for `python -m msdr.ble.capture` (docs/ble.md "Helper protocol")
used by supervisor/API tests: `[sys.executable, __file__]` as
`ble.helper_command`. Behaviour is chosen with FAKE_BLE_MODE:

- ok (default): `ready`, then the sample reports below in a loop every 20 ms
  with a `stats` line each second, until SIGTERM -> `stopped`, exit 0
- no_device: error + exit 1        - claimed: error + exit 2
- crash: error + exit 3 (before ready)  - no_le: error + exit 5
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

# Anonymised shapes of what the BCM20702A0 heard on 2026-10-02: an Apple
# Continuity frame from a resolvable private address, a connectable device
# with a 128-bit service, a static-address beacon with a name, and a legacy
# Remote ID advertisement (service data 0xFFFA, app code 0x0D).
REPORTS = [
    {
        "addr": "4a:34:3f:50:52:88",
        "addr_type": "random",
        "evt": "ADV_IND",
        "rssi": -66,
        "data": "02011a0cff4c001007361f84b53cae28020a00",
    },
    {
        "addr": "f5:53:6b:f8:c2:0b",
        "addr_type": "random",
        "evt": "ADV_IND",
        "rssi": -73,
        "data": "020106020a0411079ecadc240ee5a9e093f3a3b50100406e",
    },
    {
        "addr": "c0:11:22:33:44:55",
        "addr_type": "random",
        "evt": "ADV_NONCONN_IND",
        "rssi": -50,
        "data": "0201060909546573744265616e03ff5900",
    },
    {
        "addr": "00:11:22:33:44:66",
        "addr_type": "public",
        "evt": "ADV_NONCONN_IND",
        "rssi": -61,
        "data": "1e16faff0d00" + "00" * 25,
    },
]

DEVICE = {
    "vid": "19ff",
    "pid": "0239",
    "product": "BCM20702A0",
    "manufacturer": "Broadcom Corp",
    "serial": None,
    "bus": 0,
    "address": 18,
    "known": True,
}


def _event(**fields) -> None:
    sys.stderr.write(json.dumps(fields) + "\n")
    sys.stderr.flush()


def main() -> int:
    mode = os.environ.get("FAKE_BLE_MODE", "ok")
    if "--list" in sys.argv:
        if mode == "no_device":
            return 0
        print(json.dumps(DEVICE), flush=True)
        return 0
    if mode == "no_device":
        _event(event="error", code="no_device", message="no Bluetooth-class USB device found")
        return 1
    if mode == "claimed":
        _event(event="error", code="claimed", message="could not claim any Bluetooth dongle")
        return 2
    if mode == "no_le":
        _event(event="error", code="no_le", message="controller rejected passive LE scanning")
        return 5
    if mode == "crash":
        # A crash during bring-up (before `ready`): the exhaustion path. A
        # crash after `ready` resets the retry counter by design, like Wi-Fi.
        _event(event="error", code="usb", message="read failed: simulated")
        return 3
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))
    _event(
        event="ready",
        **DEVICE,
        hci_version=6,
        hci_revision=0x2000,
        lmp_version=6,
        lmp_subversion=0x410E,
        manufacturer_id=15,
        manufacturer_name="Broadcom",
        le_features="0100000000000000",
        scan_type="passive",
        scan_interval_ms=10,
        scan_window_ms=10,
        helper_version="test",
    )
    n = 0
    last_stats = time.monotonic()
    while not stop["flag"]:
        report = dict(REPORTS[n % len(REPORTS)], ts=time.time())
        sys.stdout.write(json.dumps(report) + "\n")
        sys.stdout.flush()
        n += 1
        if time.monotonic() - last_stats >= 1.0:
            last_stats = time.monotonic()
            _event(event="stats", reports=n)
        time.sleep(0.02)
    _event(event="stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
