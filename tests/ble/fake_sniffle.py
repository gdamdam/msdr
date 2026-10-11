"""Fake Sniffle serial device (docs/ble.md, development notes "Wi-Fi and BLE
receivers" Sniffle backend): speaks the real wire framing from
`msdr.ble.sniffle_proto`, injected directly as `sniffle_capture.capture()`'s
`ser=` argument (the "injectable stream" option -- no OS pty, so this also
runs inside a filesystem/device sandbox that refuses `os.openpty()`).
Synchronous and single-threaded: every `write()` decodes the command and
immediately appends the real-wire-framed reply (if any) to the read buffer,
and `read()` manufactures one fresh advertising report whenever the buffer
is empty, so the capture helper's own framing/decode code
(`msdr.ble.sniffle_proto`) is exercised end to end, not a stand-in JSON
stream (contrast `tests/ble/fake_helper.py`, which bypasses the wire
protocol entirely).

Modes:
- "ok" (default): answers the marker and version handshake as Sniffle
  v1.11.0, then yields a legacy ADV_NONCONN_IND report on every read.
- "firmware_mismatch": echoes the marker back with the wrong payload and
  never answers the version probe -- framed messages arrive, but nothing a
  real Sniffle would send.
- "wrong_baud": every write produces non-base64 garbage output, so the
  handshake never decodes a single valid frame (what a real baud mismatch
  looks like on the wire).
- "unplug": behaves like "ok" for a few reports, then every further
  read/write raises `serial.SerialException` (a vanished device).
"""

from __future__ import annotations

import argparse
import os
import sys
from struct import pack

from serial import SerialException

from msdr.ble import sniffle_proto as proto

FIRMWARE_VERSION = (1, 11, 0)
API_LEVEL = 10
UNPLUG_AFTER_REPORTS = 5


def _legacy_packet_body(rssi: int = -50, chan: int = 37) -> bytes:
    """One ADV_NONCONN_IND, 1M PHY, from `00:11:22:33:44:55`: flags + local
    name "TestBean" + a manufacturer-data AD structure (shape of
    tests/ble/fake_helper.py's REPORTS[2])."""
    adva_wire = bytes.fromhex("554433221100")  # little-endian on the wire
    adv_data = bytes.fromhex("0201060909546573744265616e03ff5900")
    pdu_hdr = bytes([0x02])  # pdu_type=2 (ADV_NONCONN_IND), TxAdd=0 (public)
    pkt_body = pdu_hdr + bytes([len(adva_wire) + len(adv_data)]) + adva_wire + adv_data
    chan_phy = (proto.PhyMode.PHY_1M << 6) | chan
    header = pack("<LHHbB", 0, len(pkt_body), 0, rssi, chan_phy)
    return header + pkt_body


class FakeSniffle:
    """Minimal in-process double for pyserial's `Serial`: `.read`, `.write`,
    `.baudrate`, `.close`, `.reset_input_buffer` -- everything
    `sniffle_capture.capture(..., ser=...)` calls."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.baudrate = 2_000_000
        self.closed = False
        self.reports_sent = 0
        self._in = bytearray()
        self._cmd_buf = bytearray()

    # --- pyserial surface ----------------------------------------------------

    def reset_input_buffer(self) -> None:
        self._in.clear()

    def read(self, n: int) -> bytes:
        if self.closed:
            raise SerialException("device closed")
        if not self._in and self.mode in ("ok", "unplug"):
            self._queue_report()
        data = bytes(self._in[:n])
        del self._in[:n]
        return data

    def write(self, data: bytes) -> int:
        if self.closed:
            raise SerialException("device closed")
        if self.mode == "wrong_baud":
            # A real baud mismatch corrupts every byte; simulate that by
            # never producing a line that decodes as valid base64.
            self._in.extend(b"\xff\x00garbage-not-base64\xfe\n")
            return len(data)
        self._cmd_buf.extend(data)
        while True:
            nl = self._cmd_buf.find(b"\n")
            if nl == -1:
                break
            line = bytes(self._cmd_buf[: nl + 1])
            del self._cmd_buf[: nl + 1]
            try:
                msg = proto.decode_line(line)
            except proto.FramingError:
                continue
            if msg is not None:
                self._handle(*msg)
        return len(data)

    def close(self) -> None:
        self.closed = True

    # --- fake firmware behaviour -----------------------------------------------

    def _respond(self, payload: list) -> None:
        self._in.extend(proto.frame(bytes(payload)))

    def _handle(self, opcode: int, args: bytes) -> None:
        if opcode == proto.CMD_MARKER:
            if self.mode == "firmware_mismatch":
                self._respond([proto.MSG_MARKER, *pack("<L", 0), *b"NOPE"])
            else:
                self._respond([proto.MSG_MARKER, *pack("<L", 0), *args])
        elif opcode == proto.CMD_VERSION:
            if self.mode == "firmware_mismatch":
                return  # a non-Sniffle firmware would not answer this opcode either
            major, minor, revision = FIRMWARE_VERSION
            self._respond(
                [
                    proto.MSG_MEASUREMENT,
                    4,
                    proto.MEASUREMENT_TYPE_VERSION,
                    major,
                    minor,
                    revision,
                    API_LEVEL,
                ]
            )
        # Other commands (chan/aa/phy, rssi, pause_done, follow, auxadv, mac,
        # crc_valid) configure the sniffer; the real firmware does not ack
        # them, so the fake does not either.

    def _queue_report(self) -> None:
        if self.mode == "unplug" and self.reports_sent >= UNPLUG_AFTER_REPORTS:
            self.closed = True
            raise SerialException("device disconnected")
        self._in.extend(proto.frame(bytes([proto.MSG_PACKET, *_legacy_packet_body()])))
        self.reports_sent += 1


def make_fake_sniffle(mode: str = "ok") -> FakeSniffle:
    return FakeSniffle(mode)


def main(argv: list[str] | None = None) -> int:
    """Subprocess entry point: `[sys.executable, __file__, --port ... --baud
    ... --phy ... --channel ...]` as `BleSupervisor`'s `helper_command`
    (`FAKE_SNIFFLE_MODE` env var picks the mode) -- runs the real
    `msdr.ble.sniffle_capture.capture()` against an injected `FakeSniffle`
    instead of a real serial port, so BleSupervisor's actual subprocess/pipe
    handling is exercised end to end (same role as tests/ble/fake_helper.py
    for the HCI backend, which stands in for the whole helper instead)."""
    import serial
    from serial.tools.list_ports import comports

    from msdr.ble import sniffle_capture as sc

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="FAKE")
    parser.add_argument("--baud", type=int, default=None)
    parser.add_argument("--phy", default="1m")
    parser.add_argument("--channel", default="37")
    args = parser.parse_args(argv)
    mode = os.environ.get("FAKE_SNIFFLE_MODE", "ok")
    return sc.capture(serial, comports, args, ser=FakeSniffle(mode))


if __name__ == "__main__":
    sys.exit(main())
