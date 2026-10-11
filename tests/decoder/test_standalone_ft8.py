import asyncio
import sys

import numpy as np
import pytest

from msdr.decoder.backends import DecoderError
from msdr.decoder.standalone_ft8 import (
    StandaloneFT8Backend,
    parse_jt9_line,
)


def test_parse_jt9_ft8_row():
    event = parse_jt9_line(
        "000001 -10  0.0 1500 ~  K1ABC W9XYZ EN37                        ",
        window_epoch=1_700_000_000,
    )
    assert event is not None
    assert event.text == "K1ABC W9XYZ EN37"
    assert event.fields == {
        "utc": "000001",
        "snr_db": -10,
        "time_offset_s": 0.0,
        "audio_frequency_hz": 1500,
        "mode": "FT8",
        "window_epoch": 1_700_000_000,
    }
    assert event.ts == 1_700_000_000
    assert event.integrity == "passed"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "WSJT-X decoder chatter",
        "0000  -10  0.1  1500 ~",
        "0000  -10  0.1  1500 ~ CQ K1ABC FN42 JT9",
        "0000  -10  0.1  1500 @ CQ K1ABC FN42",
    ],
)
def test_parse_jt9_ignores_non_ft8_or_invalid_rows(line):
    assert parse_jt9_line(line, window_epoch=0) is None


def test_ft8_rejects_unsupported_rate_and_missing_decoder():
    missing = StandaloneFT8Backend(None, sample_rate_hz=48_000, center_hz=0, frequency_hz=0)
    with pytest.raises(DecoderError, match="jt9"):
        asyncio.run(missing.start())
    wrong_rate = StandaloneFT8Backend("jt9", sample_rate_hz=44_100, center_hz=0, frequency_hz=0)
    with pytest.raises(DecoderError, match="integer multiple"):
        asyncio.run(wrong_rate.start())


@pytest.mark.asyncio
async def test_ft8_windows_are_mono_12khz_wav_and_decoded(tmp_path):
    # A tiny executable stand-in verifies the real asynchronous WAV/process
    # integration without pretending to validate WSJT-X RF decoding.
    helper = tmp_path / "jt9-fixture.py"
    helper.write_text(
        "import sys, wave\n"
        "with wave.open(sys.argv[-1], 'rb') as f:\n"
        " assert f.getnchannels() == 1 and f.getframerate() == 12000\n"
        " assert f.getnframes() == 180000\n"
        "print('000000 -10  0.1 1500 ~  CQ K1ABC FN42')\n"
    )
    # create_subprocess_exec takes an executable; use a small shim that
    # launches the Python helper.
    launcher = tmp_path / "jt9"
    launcher.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{helper}' \"$@\"\n")
    launcher.chmod(0o755)
    backend = StandaloneFT8Backend(
        str(launcher), sample_rate_hz=48_000, center_hz=0, frequency_hz=0
    )
    await backend.start()
    try:
        epoch = 1_700_000_010.0
        backend._window_epoch = epoch
        backend._windows.put_nowait((np.zeros(180_000, dtype="<i2").tobytes(), epoch))
        event = await asyncio.wait_for(backend.events().__anext__(), timeout=3)
        assert event.text == "CQ K1ABC FN42"
        assert event.fields["window_epoch"] == epoch
    finally:
        await backend.stop()
