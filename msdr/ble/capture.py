"""`python -m msdr.ble.capture`: drive a USB Bluetooth dongle over raw HCI
and stream passive LE advertising reports as JSON lines (docs/ble.md
"Helper protocol"). Runs as a subprocess of msdr so a wedged libusb call can
never stall the server; `pyusb` is imported here only.

Receive-only by construction: the only commands sent are Reset, the two
version/feature reads, and passive scan parameters/enable. Scan type 0x00
means the controller never transmits a SCAN_REQ.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from msdr import __version__
from msdr.ble import hci

HELPER_VERSION = __version__

EXIT_OK = 0
EXIT_NO_DEVICE = 1
EXIT_CLAIM_FAILED = 2
EXIT_USB_ERROR = 3
EXIT_DEPENDENCY_MISSING = 4
EXIT_NO_LE = 5

COMMAND_TIMEOUT_S = 2.0
READ_TIMEOUT_MS = 500
STATS_EVERY_S = 1.0


def _event(**fields) -> None:
    sys.stderr.write(json.dumps(fields) + "\n")
    sys.stderr.flush()


def _error(code: str, message: str) -> None:
    _event(event="error", code=code, message=message)


def _import_usb():
    try:
        import usb.core
        import usb.util
    except ImportError:
        return None
    try:
        usb.core.find()  # forces backend discovery; raises NoBackendError without libusb
    except usb.core.NoBackendError:
        return None
    except Exception:  # noqa: BLE001 -- enumeration problems are reported later per device
        pass
    return usb


def _string(dev, index) -> str | None:
    if not index:
        return None
    try:
        import usb.util

        return usb.util.get_string(dev, index)
    except Exception:  # noqa: BLE001 -- string descriptors are optional and often unreadable on macOS
        return None


def _interface0(dev):
    try:
        cfg = dev.get_active_configuration()
    except Exception:  # noqa: BLE001
        try:
            cfg = dev[0]
        except Exception:  # noqa: BLE001
            return None
    for intf in cfg:
        if intf.bInterfaceNumber == 0 and intf.bAlternateSetting == 0:
            return intf
    return next(iter(cfg), None)


def _is_candidate(dev) -> bool:
    intf = _interface0(dev)
    intf_class = (
        (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol)
        if intf is not None
        else None
    )
    return hci.is_bluetooth_class(
        (dev.bDeviceClass, dev.bDeviceSubClass, dev.bDeviceProtocol),
        intf_class,
        (dev.idVendor, dev.idProduct),
    )


def _describe(dev) -> dict:
    key = (dev.idVendor, dev.idProduct)
    return {
        "vid": f"{dev.idVendor:04x}",
        "pid": f"{dev.idProduct:04x}",
        "product": _string(dev, dev.iProduct) or hci.KNOWN_DONGLES.get(key),
        "manufacturer": _string(dev, dev.iManufacturer),
        "serial": _string(dev, dev.iSerialNumber),
        "bus": dev.bus,
        "address": dev.address,
        "known": key in hci.KNOWN_DONGLES,
    }


def _candidates(usb, usb_id: str | None) -> list:
    wanted = None
    if usb_id:
        vid, pid = usb_id.lower().split(":")
        wanted = (int(vid, 16), int(pid, 16))
    out = []
    for dev in usb.core.find(find_all=True) or []:
        try:
            if wanted is not None:
                if (dev.idVendor, dev.idProduct) == wanted:
                    out.append(dev)
            elif _is_candidate(dev):
                out.append(dev)
        except Exception:  # noqa: BLE001 -- a device we cannot even describe is not a candidate
            continue
    return out


def list_devices(usb, *, everything: bool = False) -> int:
    """`--list`: Bluetooth candidates; `--list --all`: every USB device with
    its class triple, for diagnosing a dongle that is not recognised."""
    if not everything:
        for dev in _candidates(usb, None):
            print(json.dumps(_describe(dev)), flush=True)
        return EXIT_OK
    for dev in usb.core.find(find_all=True) or []:
        try:
            intf = _interface0(dev)
            row = _describe(dev)
            row["device_class"] = [dev.bDeviceClass, dev.bDeviceSubClass, dev.bDeviceProtocol]
            row["interface_class"] = (
                [intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol]
                if intf is not None
                else None
            )
            row["candidate"] = _is_candidate(dev)
        except Exception as exc:  # noqa: BLE001 -- show the failure instead of hiding the device
            row = {"vid": f"{dev.idVendor:04x}", "pid": f"{dev.idProduct:04x}", "error": str(exc)}
        print(json.dumps(row), flush=True)
    return EXIT_OK


class Transport:
    """One claimed HCI interface: commands on the control endpoint, events
    from the interrupt IN endpoint, reassembled into whole HCI events."""

    def __init__(self, usb, dev) -> None:
        self.usb = usb
        self.dev = dev
        self.intf = _interface0(dev)
        self.intf_number = self.intf.bInterfaceNumber if self.intf is not None else 0
        self.ep_in = 0x81
        if self.intf is not None:
            for ep in self.intf:
                if ep.bEndpointAddress & 0x80 and (ep.bmAttributes & 0x03) == 0x03:
                    self.ep_in = ep.bEndpointAddress
                    break
        self.reassembler = hci.EventReassembler()
        self.pending: list[hci.Event] = []

    def claim(self) -> str | None:
        """Returns None on success or the reason the device could not be claimed."""
        usb = self.usb
        try:
            # Linux: let go of btusb if it holds the interface. macOS has no
            # detach; a claimed device simply fails below.
            if hasattr(self.dev, "is_kernel_driver_active") and self.dev.is_kernel_driver_active(
                self.intf_number
            ):
                self.dev.detach_kernel_driver(self.intf_number)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured, which is fine
        try:
            usb.util.claim_interface(self.dev, self.intf_number)
        except usb.core.USBError as exc:
            return str(exc)
        return None

    def release(self) -> None:
        try:
            self.usb.util.release_interface(self.dev, self.intf_number)
            self.usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: BLE001 -- best effort on the way out
            pass

    def send(self, packet: bytes) -> None:
        self.dev.ctrl_transfer(0x20, 0, 0, self.intf_number, packet, timeout=1000)

    def _is_timeout(self, exc) -> bool:
        return isinstance(exc, self.usb.core.USBTimeoutError) or getattr(exc, "errno", None) in (
            60,
            110,
        )

    def read_events(self, timeout_ms: int = READ_TIMEOUT_MS) -> list[hci.Event]:
        try:
            chunk = bytes(self.dev.read(self.ep_in, 512, timeout=timeout_ms))
        except self.usb.core.USBError as exc:
            if self._is_timeout(exc):
                return []
            raise
        return self.reassembler.feed(chunk)

    def command(self, packet: bytes) -> hci.CommandComplete | None:
        """Send and wait for the matching Command Complete/Status; events that
        arrive meanwhile (advertising reports) are queued in `pending`."""
        opcode = int.from_bytes(packet[:2], "little")
        self.send(packet)
        deadline = time.monotonic() + COMMAND_TIMEOUT_S
        while time.monotonic() < deadline:
            for event in self.read_events():
                done = hci.parse_command_complete(event)
                if done is not None and done.opcode == opcode:
                    return done
                if done is None:
                    self.pending.append(event)
        return None


def _bring_up(t: Transport) -> tuple[dict, int | None]:
    """Reset, read version/features, start passive scanning. Returns the
    `ready` fields and an exit code when the controller is unusable."""
    done = t.command(hci.command(hci.OP_RESET))
    if done is None or done.status != 0:
        return {}, EXIT_USB_ERROR
    facts: dict = {}
    ver = t.command(hci.command(hci.OP_READ_LOCAL_VERSION))
    if ver is not None and ver.status == 0:
        lv = hci.parse_local_version(ver.returned)
        if lv is not None:
            facts.update(
                hci_version=lv.hci_version,
                hci_revision=lv.hci_revision,
                lmp_version=lv.lmp_version,
                lmp_subversion=lv.lmp_subversion,
                manufacturer_id=lv.manufacturer,
                manufacturer_name=lv.manufacturer_name,
            )
    feats = t.command(hci.command(hci.OP_LE_READ_LOCAL_FEATURES))
    if feats is None or feats.status != 0:
        return facts, EXIT_NO_LE
    facts["le_features"] = feats.returned[:8].hex()
    params = t.command(hci.le_set_scan_parameters(passive=True))
    if params is None or params.status != 0:
        return facts, EXIT_NO_LE
    enable = t.command(hci.le_set_scan_enable(True, filter_duplicates=False))
    if enable is None or enable.status != 0:
        return facts, EXIT_NO_LE
    facts.update(
        scan_type="passive",
        scan_interval_ms=hci.SCAN_INTERVAL_UNITS * hci.SCAN_UNIT_MS,
        scan_window_ms=hci.SCAN_INTERVAL_UNITS * hci.SCAN_UNIT_MS,
    )
    return facts, None


def _emit_reports(events: list[hci.Event], counters: dict) -> None:
    now = time.time()
    for event in events:
        for r in hci.parse_advertising_reports(event):
            counters["reports"] += 1
            sys.stdout.write(
                json.dumps(
                    {
                        "ts": now,
                        "addr": r.address,
                        "addr_type": r.address_type_name,
                        "evt": r.event_name,
                        "rssi": r.rssi,
                        "data": r.data.hex(),
                    }
                )
                + "\n"
            )
    sys.stdout.flush()


def capture(usb, usb_id: str | None) -> int:
    candidates = _candidates(usb, usb_id)
    if not candidates:
        _error(
            "no_device",
            "no Bluetooth-class USB device found"
            if not usb_id
            else f"USB device {usb_id} not found",
        )
        return EXIT_NO_DEVICE
    transport = None
    reasons = []
    for dev in candidates:
        t = Transport(usb, dev)
        reason = t.claim()
        if reason is None:
            transport = t
            break
        reasons.append(f"{dev.idVendor:04x}:{dev.idProduct:04x}: {reason}")
    if transport is None:
        _error(
            "claimed",
            "could not claim any Bluetooth dongle (in use by the system?): " + "; ".join(reasons),
        )
        return EXIT_CLAIM_FAILED

    stop = {"flag": False}

    def _on_signal(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    counters = {"reports": 0}
    try:
        try:
            facts, failure = _bring_up(transport)
        except usb.core.USBError as exc:
            _error("usb", f"bring-up failed: {exc}")
            return EXIT_USB_ERROR
        if failure is not None:
            _error(
                "no_le" if failure == EXIT_NO_LE else "usb",
                "controller rejected passive LE scanning"
                if failure == EXIT_NO_LE
                else "controller did not answer HCI_Reset",
            )
            return failure
        _event(event="ready", **_describe(transport.dev), **facts, helper_version=HELPER_VERSION)
        _emit_reports(transport.pending, counters)
        transport.pending = []
        last_stats = time.monotonic()
        while not stop["flag"]:
            try:
                events = transport.read_events()
            except usb.core.USBError as exc:
                _error("usb", f"read failed: {exc}")
                return EXIT_USB_ERROR
            if events:
                _emit_reports(events, counters)
            if time.monotonic() - last_stats >= STATS_EVERY_S:
                last_stats = time.monotonic()
                _event(event="stats", reports=counters["reports"])
        return EXIT_OK
    finally:
        try:
            transport.send(hci.le_set_scan_enable(False))
        except Exception:  # noqa: BLE001 -- the dongle may already be gone
            pass
        transport.release()
        _event(event="stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="msdr passive BLE advertising capture (raw HCI over libusb)"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list Bluetooth-class USB devices as JSON lines and exit",
    )
    parser.add_argument(
        "--all", action="store_true", help="with --list: every USB device and its class codes"
    )
    parser.add_argument(
        "--usb-id",
        help="VID:PID of the dongle to use (default: first claimable Bluetooth-class device)",
    )
    args = parser.parse_args(argv)
    usb = _import_usb()
    if usb is None:
        _error("dependency_missing", "pyusb or libusb is not available (pip install 'msdr[ble]')")
        return EXIT_DEPENDENCY_MISSING
    try:
        if args.list:
            return list_devices(usb, everything=args.all)
        return capture(usb, args.usb_id)
    except Exception as exc:  # noqa: BLE001 -- an uncaught error must not look like "no device" (exit 1)
        _error("usb", f"{type(exc).__name__}: {exc}")
        return EXIT_USB_ERROR


if __name__ == "__main__":
    sys.exit(main())
