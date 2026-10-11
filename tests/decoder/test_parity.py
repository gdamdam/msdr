"""Single vs band decode parity on the same replayed recording (S4 §2).

A synthetic 2 MS/s SigMF recording carries an AX.25 AFSK-1200 burst train
FM-modulated at +100 kHz from the centre (tests/helpers/ax25.py). One
ReplaySource plays it once; a single-mode ReplayDecodeSession (VirtualReceiver
tuned to the channel, flat NFM tap) and a band-mode one (BandDecoder with
peak detection on the source's IQ stream) decode it at the same time. Both
must produce exactly the transmitted frames, each with integrity "passed"
(AX.25 FCS checked by the backend), and nothing on a noise-only recording.

Runs once per installed AX.25 backend (direwolf, multimon-ng); skipped only
when neither resolves.
"""

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest

from msdr.decoder.registry import resolve_binaries
from msdr.decoder.replay_decode import ReplayDecodeSession
from msdr.settings import DecoderSettings
from msdr.source.virtual_receiver import VirtualReceiver
from msdr.survey.replay import ReplaySource
from tests.helpers.ax25 import burst_train_audio, fm_iq, to_ci16

FS = 2_000_000
CENTER_HZ = 144_290_000
OFFSET_HZ = 100_000
FRAMES = [
    ("N0CALL-1", "APRS", ">msdr parity one"),
    ("W1AW-9", "CQ", "second frame 12345"),
]

_BINARIES = resolve_binaries(DecoderSettings().bin_dirs)
_BACKENDS = [name for name in ("direwolf", "multimon-ng") if _BINARIES.get(name)]

pytestmark = pytest.mark.skipif(
    not _BACKENDS,
    reason="neither direwolf nor multimon-ng resolves via resolve_binaries(default bin_dirs)",
)


def _write_recording(root: Path, iq: np.ndarray, name: str) -> Path:
    """Same layout as tests/api/test_recordings.py: SigMF run + recording.json."""
    d = root / "recordings" / name
    d.mkdir(parents=True)
    stem = f"run-01_{FS}SPS_{CENTER_HZ}Hz"
    (d / f"{stem}.sigmf-data").write_bytes(to_ci16(iq))
    meta = {
        "global": {"core:datatype": "ci16_le", "core:sample_rate": FS, "core:version": "1.0.0"},
        "captures": [{"core:sample_start": 0, "core:frequency": CENTER_HZ}],
        "annotations": [],
    }
    (d / f"{stem}.sigmf-meta").write_text(json.dumps(meta))
    index = {
        "start_wall": 1_700_000_000.0,
        "end_wall": 1_700_000_000.0 + iq.size / FS,
        "preroll_s": 0,
        "runs": [
            {
                "data": f"{stem}.sigmf-data",
                "meta": f"{stem}.sigmf-meta",
                "sample_rate": FS,
                "center_hz": CENTER_HZ,
                "samples": int(iq.size),
                "start_wall": 1_700_000_000.0,
                "settings": {},
            }
        ],
    }
    (d / "recording.json").write_text(json.dumps(index))
    return d


def _burst_iq() -> np.ndarray:
    # 1 s lead-in of unmodulated carrier: the band path's peak detector must
    # confirm the channel before the first burst starts.
    audio = burst_train_audio(FRAMES, FS, lead_s=1.0, gap_s=0.5)
    audio = np.concatenate([audio, np.zeros(FS // 2, dtype=np.float32)])
    return fm_iq(audio, FS, offset_hz=OFFSET_HZ, amplitude=0.25, noise_rms=0.01)


def _noise_iq(seconds: float = 2.0) -> np.ndarray:
    rng = np.random.default_rng(7)
    n = int(seconds * FS)
    return (0.05 / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(
        np.complex64
    )


async def _decode_both(rec: Path, binaries: dict, tmp: Path) -> tuple[list, list, dict, dict]:
    source = ReplaySource(rec, loop=False)
    rx = VirtualReceiver(source)
    rx.tune(CENTER_HZ + OFFSET_HZ, "NFM", 12_500)
    await rx.start()
    single_events: list = []
    band_events: list = []
    kw = {
        "binaries": binaries,
        "settings": DecoderSettings(),
        "on_status": lambda status: None,
        "calls_dir": tmp / "calls",
        "fax_dir": tmp / "fax",
    }
    single = ReplayDecodeSession(rx, source, on_event=single_events.append, **kw)
    band = ReplayDecodeSession(rx, source, on_event=band_events.append, **kw)
    try:
        await single.start_single(["aprs"])
        await band.start_band(["aprs"], max_channels=2, threshold_db=10.0)
        await asyncio.sleep(0.1)  # let the tap pump subscribe before playback
        await source.start()
        deadline = time.monotonic() + 30
        while source.playing and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not source.playing, "replay did not finish"
        # Grace for the backends to flush the last burst.
        await asyncio.sleep(1.5)
        single_status, band_status = single.status(), band.status()
    finally:
        await single.stop()
        await band.stop()
        await source.stop()
        await rx.stop()
    return single_events, band_events, single_status, band_status


def _frames(events: list) -> set[tuple[str, str, str]]:
    return {(e.fields["from"], e.fields["to"], e.text.partition(":")[2]) for e in events}


def _binaries_for(backend: str) -> dict:
    # Force one AX.25 backend: direwolf is preferred whenever it resolves.
    other = "multimon-ng" if backend == "direwolf" else "direwolf"
    return {**_BINARIES, other: None}


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_single_and_band_decode_the_same_frames(tmp_path, backend):
    rec = _write_recording(tmp_path, _burst_iq(), "burst")
    single, band, single_status, band_status = await _decode_both(
        rec, _binaries_for(backend), tmp_path
    )
    expected = {(src, dst, info) for src, dst, info in FRAMES}
    print(f"[{backend}] single: {sorted(_frames(single))}")
    print(f"[{backend}] band:   {sorted(_frames(band))}")
    assert _frames(single) == expected
    assert _frames(band) == expected
    for event in single + band:
        assert event.integrity == "passed"
        assert "FCS" in event.integrity_note
        assert event.source_id == "replay:burst"
        assert event.analysis_run_id is not None
        assert event.generation == 0
        assert event.sample_index is not None
    assert {e.analysis_run_id for e in single} != {e.analysis_run_id for e in band}
    # Provenance: the event's input block is at/after its burst in the file.
    first_burst_at = 1.0 * FS
    assert all(e.sample_index >= first_burst_at for e in single + band)

    s = single_status["decoders"][0]
    assert s["id"] == "aprs" and s["events"] == len(single)
    assert s["valid_frames"] == len(single) and s["session_state"] == "valid_frames"
    b = band_status["decoders"][0]
    assert b["events"] == len(band) and b["session_state"] == "valid_frames"
    assert single_status["mode"] == "single" and band_status["mode"] == "band"
    assert band_status["channels"], "band path detected no channel"


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_noise_only_yields_no_events_on_either_path(tmp_path, backend):
    rec = _write_recording(tmp_path, _noise_iq(), "noise")
    single, band, single_status, _band_status = await _decode_both(
        rec, _binaries_for(backend), tmp_path
    )
    assert single == []
    assert band == []
    assert single_status["decoders"][0]["session_state"] == "acquiring"


@pytest.mark.parametrize("backend", _BACKENDS[:1])
async def test_seek_restarts_backends_without_leaking_processes(tmp_path, backend, monkeypatch):
    from msdr.decoder import backends as backends_mod

    started: list = []
    real_start = backends_mod.ProcessBackend.start

    async def tracking_start(self):
        await real_start(self)
        started.append(self)

    monkeypatch.setattr(backends_mod.ProcessBackend, "start", tracking_start)

    rec = _write_recording(tmp_path, _burst_iq(), "seek")
    source = ReplaySource(rec, loop=False)
    rx = VirtualReceiver(source)
    rx.tune(CENTER_HZ + OFFSET_HZ, "NFM", 12_500)
    await rx.start()
    single_events: list = []
    band_events: list = []
    kw = {
        "binaries": _binaries_for(backend),
        "settings": DecoderSettings(),
        "on_status": lambda status: None,
        "calls_dir": tmp_path / "calls",
        "fax_dir": tmp_path / "fax",
    }
    single = ReplayDecodeSession(rx, source, on_event=single_events.append, **kw)
    band = ReplayDecodeSession(rx, source, on_event=band_events.append, **kw)
    try:
        await single.start_single(["aprs"])
        await band.start_band(["aprs"], max_channels=2)
        await asyncio.sleep(0.1)
        await source.start()
        await asyncio.sleep(0.4)  # inside the lead-in, before any burst
        source.seek(0.0)
        deadline = time.monotonic() + 30
        while source.playing and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.5)
        single_status, band_status = single.status(), band.status()
    finally:
        await single.stop()
        await band.stop()
        await source.stop()
        await rx.stop()

    assert single_status["restarts"] >= 1
    assert band_status["restarts"] >= 1
    expected = {(src, dst, info) for src, dst, info in FRAMES}
    # Each frame decoded once, from the post-seek generation only.
    assert sorted(_frames(single_events)) == sorted(expected)
    assert len(single_events) == len(FRAMES)
    assert {e.generation for e in single_events} == {1}
    assert _frames(band_events) == expected and {e.generation for e in band_events} == {1}
    # Every process ever started (incl. the ones replaced on the seek) exited.
    assert len(started) >= 3
    assert all(b._proc.returncode is not None for b in started)


async def test_single_reports_unsupported_input_without_starting(tmp_path):
    rec = _write_recording(tmp_path, _noise_iq(0.5), "usb")
    source = ReplaySource(rec, loop=False)
    rx = VirtualReceiver(source)
    rx.tune(CENTER_HZ + OFFSET_HZ, "USB", 2_700)
    session = ReplayDecodeSession(
        rx,
        source,
        binaries=_BINARIES,
        settings=DecoderSettings(),
        on_event=lambda e: None,
        on_status=lambda s: None,
    )
    status = await session.start_single(["aprs"])
    try:
        (aprs,) = status["decoders"]
        assert aprs["session_state"] == "unsupported_input"
        assert "NFM" in aprs["unsupported"] and "USB" in aprs["unsupported"]
        assert session._singles["aprs"].backend is None
        assert status["mode"] == "single"
    finally:
        await session.stop()


async def test_clean_tap_is_pre_volume_and_pre_mute(tmp_path):
    t = np.arange(int(0.5 * FS)) / FS
    # AM carrier at +100 kHz with a 1 kHz tone.
    iq = 0.3 * (1 + 0.5 * np.sin(2 * np.pi * 1000 * t)) * np.exp(2j * np.pi * OFFSET_HZ * t)
    rec = _write_recording(tmp_path, iq.astype(np.complex64), "am")
    source = ReplaySource(rec, loop=False)
    rx = VirtualReceiver(source)
    rx.tune(CENTER_HZ + OFFSET_HZ, "AM", 10_000)
    rx.volume(0.25)
    rx.mute(True)

    async def first_clean():
        async for pcm, gen, idx in rx.clean_tap():
            return pcm, gen, idx

    reader = asyncio.create_task(first_clean())
    await asyncio.sleep(0)  # subscribe before frames flow
    frame = type("F", (), {})()
    raw = to_ci16(iq[: FS // 20].astype(np.complex64))
    frame.data, frame.generation, frame.sample_index = raw, 0, 0
    listening = rx.process_frame(frame)
    pcm, gen, idx = await asyncio.wait_for(reader, 2)
    assert (gen, idx) == (0, 0)
    assert not np.any(np.frombuffer(listening.pcm, dtype="<i2"))  # muted for the ear
    assert np.abs(np.frombuffer(pcm, dtype="<i2")).max() > 1000  # decoder still hears it
