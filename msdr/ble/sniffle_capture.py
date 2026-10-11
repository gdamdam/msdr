# SPDX-License-Identifier: GPL-3.0-or-later
"""`python -m msdr.ble.sniffle_capture`: drive a SONOFF ZBDongle-P (CC2652P)
running Sniffle firmware over its CP210x USB-serial bridge and stream passive
BLE advertising reports as JSON lines, in the same shape as
`msdr.ble.capture` (docs/ble.md "Helper protocol") plus `phy`, `channel`,
`extended`, `aux`. Runs as a subprocess of msdr, same pattern as
`msdr.ble.capture`; `pyserial` is imported here only.

Receive-only by construction: the only commands ever sent are the marker and
version probes and the channel/PHY/RSSI/CRC/MAC-filter/aux-follow/
follow-disable commands in `msdr.ble.sniffle_proto` -- that module implements
no scan, connect, advertise or transmit command at all.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time

from msdr import __version__
from msdr.ble import sniffle_proto as proto

HELPER_VERSION = __version__

EXIT_OK = 0
EXIT_NO_DEVICE = 1
EXIT_USB_ERROR = 3  # generic serial I/O error; numbered to match msdr.ble.capture
EXIT_DEPENDENCY_MISSING = 4
EXIT_FIRMWARE_MISMATCH = 6
EXIT_WRONG_BAUD = 7

#: 2 Mbaud needs the CC2652P1 firmware variant and a CP2102N bridge; 921600
#: is the fallback for CP2102 (non-N) bridges from the 2022 shortage
#: (docs/ble.md, development notes Sniffle hardware notes).
BAUD_CANDIDATES = (2_000_000, 921_600)
HANDSHAKE_TIMEOUT_S = 1.5
READ_TIMEOUT_S = 0.2
STATS_EVERY_S = 1.0
DWELL_HOP_S = 1.0
CP210X_VID = 0x10C4
CP210X_PID = 0xEA60


def _event(**fields) -> None:
    sys.stderr.write(json.dumps(fields) + "\n")
    sys.stderr.flush()


def _error(code: str, message: str) -> None:
    _event(event="error", code=code, message=message)


def _import_serial():
    try:
        import serial
        from serial.tools.list_ports import comports
    except ImportError:
        return None, None
    return serial, comports


def list_devices(comports) -> int:
    """`--list`: CP210x-class serial ports (VID 0x10c4 PID 0xea60, the SONOFF
    ZBDongle-P's bridge chip) as JSON lines, and exit 0. Non-CP210x ports are
    also listed (`known: false`) so an unrecognised bridge variant is still
    selectable."""
    for port in comports():
        known = port.vid == CP210X_VID and port.pid == CP210X_PID
        print(
            json.dumps(
                {
                    "port": port.device,
                    "vid": f"{port.vid:04x}" if port.vid is not None else None,
                    "pid": f"{port.pid:04x}" if port.pid is not None else None,
                    "manufacturer": port.manufacturer,
                    "product": port.product,
                    "serial": port.serial_number,
                    "known": known,
                }
            ),
            flush=True,
        )
    return EXIT_OK


def _find_port(comports, wanted: str | None) -> str | None:
    if wanted:
        return wanted
    for port in comports():
        if port.vid == CP210X_VID and port.pid == CP210X_PID:
            return port.device
    return None


class _LineReader:
    """Reads CRLF-terminated base64 frames off a pyserial `Serial`-like
    object. Every Sniffle message is exactly one line; partial reads persist
    across calls (a bare `Serial.readline()` would drop a line split across
    a read timeout)."""

    def __init__(self, ser) -> None:
        self.ser = ser
        self._buf = bytearray()

    def read_message(self) -> tuple[int, bytes] | None:
        """One `(msg_type, body)`, or None when no full line is buffered yet
        (a read timeout -- not an error). Raises `proto.FramingError` for a
        malformed line."""
        nl = self._buf.find(b"\n")
        if nl == -1:
            chunk = self.ser.read(256)
            if chunk:
                self._buf.extend(chunk)
            nl = self._buf.find(b"\n")
            if nl == -1:
                return None
        line = bytes(self._buf[: nl + 1])
        del self._buf[: nl + 1]
        return proto.decode_line(line)


def _handshake(ser) -> tuple[dict, int | None]:
    """Marker-then-version exchange at whatever baud `ser` is currently set
    to. Returns `(version_facts, failure_exit_code_or_None)`:
    `EXIT_WRONG_BAUD` when nothing resembling a framed message arrives at
    all, `EXIT_FIRMWARE_MISMATCH` when framed messages arrive but never a
    matching marker or a version reply (the wrong Sniffle build, or not
    Sniffle)."""
    reader = _LineReader(ser)
    marker_data = b"MSDR"
    try:
        ser.reset_input_buffer()
    except Exception:  # noqa: BLE001 -- best effort, not all transports support it
        pass
    ser.write(proto.cmd_marker(marker_data))

    saw_any_frame = False
    marker_ok = False
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
    while time.monotonic() < deadline and not marker_ok:
        try:
            msg = reader.read_message()
        except proto.FramingError:
            continue
        if msg is None:
            continue
        saw_any_frame = True
        mtype, body = msg
        if mtype == proto.MSG_MARKER and body[4:] == marker_data:
            marker_ok = True
    if not marker_ok:
        return {}, EXIT_WRONG_BAUD if not saw_any_frame else EXIT_FIRMWARE_MISMATCH

    ser.write(proto.cmd_version())
    version = None
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
    while time.monotonic() < deadline and version is None:
        try:
            msg = reader.read_message()
        except proto.FramingError:
            continue
        if msg is None:
            continue
        mtype, body = msg
        if mtype == proto.MSG_MEASUREMENT:
            version = proto.decode_measurement(body)
    if version is None:
        return {}, EXIT_FIRMWARE_MISMATCH
    return version, None


def capture(serial_mod, comports, args, ser=None, stop: dict | None = None) -> int:
    """`ser`: an already-open serial-like object (`.read`, `.write`,
    `.baudrate`, `.close`, `.reset_input_buffer`), for tests
    (tests/ble/fake_sniffle.py) that inject a fake device directly instead
    of opening a real port. `stop`: a shared `{"flag": bool}` a test can set
    from another thread instead of a real signal (registering a SIGTERM
    handler only works from a process's main thread). Production calls
    always leave both None."""
    baud_candidates = (args.baud,) if args.baud else BAUD_CANDIDATES
    if ser is None:
        port = _find_port(comports, args.port)
        if port is None:
            _error(
                "no_device",
                "no CP210x serial port found"
                if not args.port
                else f"serial port {args.port} not found",
            )
            return EXIT_NO_DEVICE
        try:
            ser = serial_mod.Serial(port, baud_candidates[0], timeout=READ_TIMEOUT_S)
        except (OSError, serial_mod.SerialException) as exc:
            _error("no_device", f"could not open {port}: {exc}")
            return EXIT_NO_DEVICE
    else:
        port = args.port or "(injected)"

    phy = proto.PHY_BY_NAME[args.phy]
    channels = (37, 38, 39) if args.channel == "hop" else (int(args.channel),)

    facts: dict = {}
    chosen_baud = None
    wrong_baud_seen = False
    try:
        for baud in baud_candidates:
            ser.baudrate = baud
            facts, failure = _handshake(ser)
            if failure is None:
                chosen_baud = baud
                break
            if failure == EXIT_WRONG_BAUD:
                wrong_baud_seen = True
                continue
            ser.close()
            _error(
                "firmware_mismatch",
                "firmware does not answer as Sniffle -- flash the matching Sniffle "
                "build for this dongle's bridge chip (CP2102 vs CP2102N); msdr never "
                "flashes firmware",
            )
            return EXIT_FIRMWARE_MISMATCH
        else:
            ser.close()
            if wrong_baud_seen:
                _error(
                    "wrong_baud",
                    "no response at "
                    + ", ".join(str(b) for b in baud_candidates)
                    + " baud -- check CP2102 vs CP2102N and try the other rate",
                )
                return EXIT_WRONG_BAUD
            _error("no_device", f"no response from {port}")
            return EXIT_NO_DEVICE
    except serial_mod.SerialException as exc:
        _error("serial", f"handshake failed: {exc}")
        return EXIT_USB_ERROR

    if stop is None:
        stop = {"flag": False}

        def _on_signal(signum, frame):  # noqa: ARG001
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    def _configure(chan: int) -> None:
        ser.write(proto.cmd_chan_aa_phy(chan, phy))
        ser.write(proto.cmd_rssi(-128))
        ser.write(proto.cmd_pause_done(False))
        ser.write(proto.cmd_follow_disabled())
        ser.write(proto.cmd_auxadv(True))
        ser.write(proto.cmd_mac_clear())
        ser.write(proto.cmd_crc_valid(True))

    reader = _LineReader(ser)
    counters = {"reports": 0}
    chan_idx = 0
    try:
        _configure(channels[chan_idx])
        _event(
            event="ready",
            firmware_version="{}.{}.{}".format(
                facts["major"], facts["minor"], facts["revision"]
            ),
            api_level=facts["api_level"],
            port=port,
            baud=chosen_baud,
            phy=args.phy,
            channel=args.channel,
            extended=True,
            helper_version=HELPER_VERSION,
        )
        last_stats = time.monotonic()
        last_hop = time.monotonic()
        while not stop["flag"]:
            try:
                msg = reader.read_message()
            except proto.FramingError:
                msg = None
            except serial_mod.SerialException as exc:
                # pyserial's own message already starts "read failed: ..." on
                # an unplug; don't print the prefix twice.
                text = str(exc)
                _error("serial", text if text.startswith("read failed") else f"read failed: {text}")
                return EXIT_USB_ERROR
            if msg is not None:
                mtype, body = msg
                if mtype == proto.MSG_PACKET:
                    report = proto.decode_packet_message(body)
                    if report is not None:
                        counters["reports"] += 1
                        sys.stdout.write(json.dumps({"ts": time.time(), **report}) + "\n")
                        sys.stdout.flush()
            now = time.monotonic()
            if len(channels) > 1 and now - last_hop >= DWELL_HOP_S:
                last_hop = now
                chan_idx = (chan_idx + 1) % len(channels)
                try:
                    ser.write(proto.cmd_chan_aa_phy(channels[chan_idx], phy))
                except serial_mod.SerialException as exc:
                    _error("serial", f"write failed: {exc}")
                    return EXIT_USB_ERROR
            if now - last_stats >= STATS_EVERY_S:
                last_stats = now
                _event(event="stats", reports=counters["reports"])
        return EXIT_OK
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001 -- the dongle may already be gone
            pass
        _event(event="stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="msdr passive BLE advertising capture (Sniffle over serial)"
    )
    parser.add_argument(
        "--list", action="store_true", help="list CP210x-class serial ports as JSON lines and exit"
    )
    parser.add_argument("--port", help="serial port (default: first CP210x port found)")
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="serial baud rate (default: try 2000000, then 921600)",
    )
    parser.add_argument("--phy", choices=sorted(proto.PHY_BY_NAME), default="1m")
    parser.add_argument("--channel", choices=["37", "38", "39", "hop"], default="37")
    args = parser.parse_args(argv)
    serial_mod, comports = _import_serial()
    if serial_mod is None:
        _error("dependency_missing", "pyserial is not available (pip install 'msdr[ble]')")
        return EXIT_DEPENDENCY_MISSING
    try:
        if args.list:
            return list_devices(comports)
        return capture(serial_mod, comports, args)
    except Exception as exc:  # noqa: BLE001 -- an uncaught error must not look like "no device" (exit 1)
        _error("serial", f"{type(exc).__name__}: {exc}")
        return EXIT_USB_ERROR


if __name__ == "__main__":
    sys.exit(main())
