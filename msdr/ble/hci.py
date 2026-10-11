"""HCI command/event encoding for a USB Bluetooth controller (docs/ble.md).

Pure functions only: `pyusb` is imported by `msdr.ble.capture`, never here,
so the parser and the supervisor import cleanly on machines without libusb.
Byte layouts follow the Bluetooth Core Specification, Vol 4 Part E (HCI).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# USB class triple for a Bluetooth HCI transport (Wireless Controller / RF /
# Bluetooth Programming Interface); may be on the device or on interface 0.
BLUETOOTH_USB_CLASS = (0xE0, 0x01, 0x01)

#: (vid, pid) -> product name for dongles known to work over raw HCI.
KNOWN_DONGLES: dict[tuple[int, int], str] = {
    (0x19FF, 0x0239): "BCM20702A0 (Rocketfish/Dynex)",
    (0x0A5C, 0x21E8): "BCM20702A0",
    (0x0A5C, 0x21EC): "BCM20702A0",
    (0x0A5C, 0x21E6): "BCM20702A0",
    (0x0A5C, 0x21E1): "BCM20702A0",
    (0x0B05, 0x17CB): "BCM20702A0 (ASUS USB-BT400)",
}

# Opcodes: OGF << 10 | OCF.
OP_RESET = 0x0C03
OP_READ_LOCAL_VERSION = 0x1001
OP_LE_READ_LOCAL_FEATURES = 0x2003
OP_LE_SET_SCAN_PARAMETERS = 0x200B
OP_LE_SET_SCAN_ENABLE = 0x200C

EVT_COMMAND_COMPLETE = 0x0E
EVT_COMMAND_STATUS = 0x0F
EVT_LE_META = 0x3E
SUBEVT_LE_ADVERTISING_REPORT = 0x02

# Scan type 0x00 is passive: the controller never sends SCAN_REQ.
SCAN_TYPE_PASSIVE = 0x00
#: 10 ms in 0.625 ms units; interval == window means 100 % duty.
SCAN_INTERVAL_UNITS = 0x0010
SCAN_UNIT_MS = 0.625

ADV_EVENT_NAMES = {
    0: "ADV_IND",
    1: "ADV_DIRECT_IND",
    2: "ADV_SCAN_IND",
    3: "ADV_NONCONN_IND",
    4: "SCAN_RSP",
}

# Bluetooth SIG company identifiers (Assigned Numbers) -- the ones that
# make up most of what a dongle hears; everything else is shown numerically.
MANUFACTURER_NAMES = {
    0: "Ericsson",
    10: "Cambridge Silicon Radio",
    13: "Texas Instruments",
    15: "Broadcom",
    89: "Nordic Semiconductor",
    93: "Realtek",
    305: "Cypress Semiconductor",
    1452: "Apple",
}


#: Broadcom (and Intel) OEM dongles declare a vendor-specific class with the
#: Bluetooth subclass/protocol instead of 0xE0; Linux btusb matches this too.
VENDOR_BLUETOOTH_USB_CLASS = (0xFF, 0x01, 0x01)


def is_bluetooth_class(
    device_class: tuple[int, int, int],
    interface_class: tuple[int, int, int] | None,
    usb_id: tuple[int, int] | None = None,
) -> bool:
    """True when the device looks like a Bluetooth HCI transport: standard
    class on the device or its first interface, the Broadcom vendor-specific
    convention, or a known dongle id."""
    if usb_id is not None and usb_id in KNOWN_DONGLES:
        return True
    return any(
        c in (BLUETOOTH_USB_CLASS, VENDOR_BLUETOOTH_USB_CLASS)
        for c in (device_class, interface_class)
        if c is not None
    )


def command(opcode: int, params: bytes = b"") -> bytes:
    """An HCI command packet (sent on the control endpoint, no H4 indicator)."""
    if len(params) > 255:
        raise ValueError("HCI command parameters exceed 255 bytes")
    return struct.pack("<HB", opcode, len(params)) + params


def le_set_scan_parameters(
    *, passive: bool = True, interval: int = SCAN_INTERVAL_UNITS, window: int = SCAN_INTERVAL_UNITS
) -> bytes:
    scan_type = SCAN_TYPE_PASSIVE if passive else 0x01
    # own_address_type public (0), filter policy accept all (0)
    return command(
        OP_LE_SET_SCAN_PARAMETERS, struct.pack("<BHHBB", scan_type, interval, window, 0x00, 0x00)
    )


def le_set_scan_enable(enable: bool, *, filter_duplicates: bool = False) -> bytes:
    return command(OP_LE_SET_SCAN_ENABLE, bytes([int(enable), int(filter_duplicates)]))


@dataclass(frozen=True)
class Event:
    code: int
    params: bytes


class EventReassembler:
    """Turns the byte stream read from the interrupt endpoint into whole HCI
    events. USB interrupt transfers normally deliver one event each, but an
    event can also arrive split (16-byte max-packet controllers) or two can
    arrive back to back; buffering by the length header handles both."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Event]:
        self._buf += chunk
        out: list[Event] = []
        while len(self._buf) >= 2:
            length = self._buf[1]
            if len(self._buf) < 2 + length:
                break
            out.append(Event(self._buf[0], bytes(self._buf[2 : 2 + length])))
            del self._buf[: 2 + length]
        return out


@dataclass(frozen=True)
class CommandComplete:
    opcode: int
    status: int
    returned: bytes


def parse_command_complete(event: Event) -> CommandComplete | None:
    """Command Complete (0x0E) or Command Status (0x0F) folded into one shape;
    None for anything else."""
    if event.code == EVT_COMMAND_COMPLETE and len(event.params) >= 4:
        opcode = struct.unpack_from("<H", event.params, 1)[0]
        return CommandComplete(opcode, event.params[3], event.params[4:])
    if event.code == EVT_COMMAND_STATUS and len(event.params) >= 4:
        opcode = struct.unpack_from("<H", event.params, 2)[0]
        return CommandComplete(opcode, event.params[0], b"")
    return None


@dataclass(frozen=True)
class LocalVersion:
    hci_version: int
    hci_revision: int
    lmp_version: int
    manufacturer: int
    lmp_subversion: int

    @property
    def manufacturer_name(self) -> str | None:
        return MANUFACTURER_NAMES.get(self.manufacturer)


def parse_local_version(returned: bytes) -> LocalVersion | None:
    if len(returned) < 8:
        return None
    hci_ver, hci_rev, lmp_ver, manuf, lmp_sub = struct.unpack_from("<BHBHH", returned, 0)
    return LocalVersion(hci_ver, hci_rev, lmp_ver, manuf, lmp_sub)


@dataclass(frozen=True)
class AdvertisingReport:
    event_type: int
    address_type: int  # 0 public, 1 random (2/3: identity addresses, resolved by the controller)
    address: str  # "aa:bb:cc:dd:ee:ff", most significant byte first
    data: bytes
    rssi: int | None  # None when the controller reported 127 (not available)

    @property
    def event_name(self) -> str:
        return ADV_EVENT_NAMES.get(self.event_type, f"type {self.event_type}")

    @property
    def address_type_name(self) -> str:
        return "public" if self.address_type in (0, 2) else "random"


def parse_advertising_reports(event: Event) -> list[AdvertisingReport]:
    """LE Advertising Report sub-events (0x3E / 0x02) -> reports; anything
    else, including a truncated report list, yields what could be parsed."""
    p = event.params
    if event.code != EVT_LE_META or len(p) < 2 or p[0] != SUBEVT_LE_ADVERTISING_REPORT:
        return []
    out: list[AdvertisingReport] = []
    off = 2
    for _ in range(p[1]):
        if off + 9 > len(p):
            break
        event_type, address_type = p[off], p[off + 1]
        address = p[off + 2 : off + 8][::-1].hex(":")
        length = p[off + 8]
        data_end = off + 9 + length
        if data_end + 1 > len(p):
            break
        data = p[off + 9 : data_end]
        rssi_raw = struct.unpack_from("<b", p, data_end)[0]
        out.append(
            AdvertisingReport(
                event_type, address_type, address, data, None if rssi_raw == 127 else rssi_raw
            )
        )
        off = data_end + 1
    return out
