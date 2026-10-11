"""`msdr.ble.sniffle_capture` against `tests/ble/fake_sniffle.py` (speaks the
real wire framing, injected in place of a real serial port -- see that
module's docstring for why there is no OS pty here)."""

from __future__ import annotations

import json
import sys
import threading

import serial
from serial.tools.list_ports import comports

from msdr.ble import sniffle_capture as sc
from tests.ble.fake_sniffle import FakeSniffle


class _Args:
    def __init__(self, port="FAKE", baud=2_000_000, phy="1m", channel="37"):
        self.port = port
        self.baud = baud
        self.phy = phy
        self.channel = channel


def _run(mode: str, *, args: _Args | None = None, settle_s: float = 0.15):
    """Runs `capture()` in a background thread against a fresh FakeSniffle,
    stops it after `settle_s`, and returns (exit_code, stdout_lines,
    stderr_events)."""
    fake = FakeSniffle(mode)
    stop = {"flag": False}
    out_lines: list[str] = []
    err_events: list[dict] = []
    rc_holder: dict = {}

    class _Out:
        def write(self, s):
            out_lines.extend(line for line in s.splitlines() if line)

        def flush(self):
            pass

    class _Err:
        def write(self, s):
            for line in s.splitlines():
                if line:
                    err_events.append(json.loads(line))

        def flush(self):
            pass

    def target():
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Out(), _Err()
        try:
            rc_holder["rc"] = sc.capture(serial, comports, args or _Args(), ser=fake, stop=stop)
        finally:
            sys.stdout, sys.stderr = real_out, real_err

    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=settle_s)
    if t.is_alive():
        stop["flag"] = True
        t.join(timeout=5.0)
    assert not t.is_alive(), "capture() did not stop"
    return rc_holder["rc"], out_lines, err_events, fake


def test_ok_mode_emits_ready_then_reports_then_stopped():
    rc, out_lines, err_events, _ = _run("ok", settle_s=1.2)
    assert rc == sc.EXIT_OK
    ready = next(e for e in err_events if e["event"] == "ready")
    assert ready["firmware_version"] == "1.11.0"
    assert ready["phy"] == "1m"
    assert ready["channel"] == "37"
    assert ready["extended"] is True
    assert err_events[-1]["event"] == "stopped"
    assert any(e["event"] == "stats" for e in err_events)
    assert out_lines
    report = json.loads(out_lines[0])
    assert report["addr"] == "00:11:22:33:44:55"
    assert report["phy"] == "1m" and report["channel"] == 37 and report["extended"] is False


def test_firmware_mismatch_never_emits_ready():
    rc, out_lines, err_events, _ = _run("firmware_mismatch", settle_s=3.0)
    assert rc == sc.EXIT_FIRMWARE_MISMATCH
    assert not out_lines
    assert not any(e["event"] == "ready" for e in err_events)
    error = next(e for e in err_events if e["event"] == "error")
    assert error["code"] == "firmware_mismatch"


def test_wrong_baud_tries_both_candidates_then_fails():
    rc, out_lines, err_events, _ = _run(
        "wrong_baud", args=_Args(baud=None), settle_s=4.0
    )
    assert rc == sc.EXIT_WRONG_BAUD
    assert not out_lines
    error = next(e for e in err_events if e["event"] == "error")
    assert error["code"] == "wrong_baud"
    assert "2000000" in error["message"] and "921600" in error["message"]


def test_unplug_mid_run_reports_serial_error():
    rc, out_lines, err_events, fake = _run("unplug", settle_s=2.0)
    assert rc == sc.EXIT_USB_ERROR
    assert len(out_lines) == fake.reports_sent
    error = next(e for e in err_events if e["event"] == "error")
    assert error["code"] == "serial"


def test_list_devices_flags_cp210x(monkeypatch, capsys):
    class _Port:
        def __init__(self, device, vid, pid):
            self.device, self.vid, self.pid = device, vid, pid
            self.manufacturer = "Silicon Labs"
            self.product = "Sonoff Zigbee 3.0 USB Dongle Plus"
            self.serial_number = "0001"

    ports = [_Port("/dev/cu.usbserial-FAKE", 0x10C4, 0xEA60), _Port("/dev/cu.other", 0x0, 0x0)]
    rc = sc.list_devices(lambda: ports)
    assert rc == sc.EXIT_OK
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert rows[0]["known"] is True and rows[0]["port"] == "/dev/cu.usbserial-FAKE"
    assert rows[1]["known"] is False
