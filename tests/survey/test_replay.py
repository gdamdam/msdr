"""IQ-recording replay (msdr.survey.replay): file readers and spectrum."""

import json
import struct

import numpy as np
import pytest

from msdr.survey.replay import IQFile, ReplayError, ReplaySource, spectrum_db


def _iq16(n: int, rate: float, tone_hz: float, amp: float = 0.4) -> np.ndarray:
    t = np.arange(n) / rate
    z = amp * np.exp(2j * np.pi * tone_hz * t)
    out = np.empty(2 * n, dtype="<i2")
    out[0::2] = np.round(z.real * 32767)
    out[1::2] = np.round(z.imag * 32767)
    return out


def write_wav(path, iq16: np.ndarray, rate: int, *, rf64: bool = False) -> None:
    data = iq16.tobytes()
    fmt = struct.pack("<HHIIHH", 1, 2, rate, rate * 4, 4, 16)
    if rf64:
        # ds64: riff size, data size, sample count, table length
        ds64 = struct.pack("<QQQI", 0, len(data), len(data) // 4, 0)
        chunks = b"ds64" + struct.pack("<I", len(ds64)) + ds64
        head, data_size = b"RF64", 0xFFFFFFFF
    else:
        # SDRconnect writes a JUNK placeholder where RF64's ds64 would go.
        chunks = b"JUNK" + struct.pack("<I", 28) + bytes(28)
        head, data_size = b"RIFF", len(data)
    chunks += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", data_size) + data
    path.write_bytes(head + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + chunks)


@pytest.mark.parametrize("rf64", [False, True])
def test_wav_reader(tmp_path, rf64):
    rate = 1_000_000
    iq = _iq16(50_000, rate, 100_000)
    path = tmp_path / "SDRconnect_IQ_20260925_135725_13000000HZ.wav"
    write_wav(path, iq, rate, rf64=rf64)
    f = IQFile(path)
    assert (f.center_hz, f.sample_rate_hz, f.total_samples) == (13_000_000, rate, 50_000)
    assert f.duration_s == pytest.approx(0.05)
    assert np.array_equal(f.read(10, 5), iq[20:30])
    assert f.read(49_998, 10).size == 4  # clipped at the end


def test_wav_without_centre_in_name_is_refused(tmp_path):
    path = tmp_path / "capture.wav"
    write_wav(path, _iq16(1000, 1e6, 0), 1_000_000)
    with pytest.raises(ReplayError, match="centre frequency"):
        IQFile(path)


def test_sigmf_directory_plays_segments_in_order(tmp_path):
    rate = 500_000
    for i in range(2):
        iq = np.full(2000, i + 1, dtype="<i2")
        (tmp_path / f"seg{i}.sigmf-data").write_bytes(iq.tobytes())
        meta = {
            "global": {"core:datatype": "ci16_le", "core:sample_rate": rate},
            "captures": [{"core:sample_start": 0, "core:frequency": 146_000_000}],
        }
        (tmp_path / f"seg{i}.sigmf-meta").write_text(json.dumps(meta))
    f = IQFile(tmp_path)
    assert (f.center_hz, f.sample_rate_hz, f.total_samples) == (146_000_000, rate, 2000)
    across = f.read(998, 4)  # last 2 of segment 0, first 2 of segment 1
    assert across.tolist() == [1, 1, 1, 1, 2, 2, 2, 2]


def test_spectrum_puts_a_tone_in_its_bin():
    rate = 1_024_000
    tone = 200_000  # +200 kHz -> bin 256 + 100
    db = spectrum_db(_iq16(512 * 32, rate, tone))
    assert int(np.argmax(db)) == 256 + round(tone / (rate / 512))


def test_replay_source_levels_and_bins(tmp_path):
    rate = 1_000_000
    path = tmp_path / "x_100000000HZ.wav"
    rng = np.random.default_rng(1)
    iq = _iq16(rate, rate, 150_000, amp=0.3)
    iq = (iq + rng.normal(scale=30, size=iq.size)).astype("<i2")
    write_wav(path, iq, rate)
    src = ReplaySource(path)
    assert src.ref_level_db > src.base_db
    bins = np.frombuffer(src._to_bins(src.file.read(0, src._block)), dtype=np.uint8)
    assert bins.size == 512
    assert int(np.argmax(bins)) == 256 + round(150_000 / (rate / 512))
    assert int(np.median(bins)) < int(bins.max()) - 100  # tone well above the floor
