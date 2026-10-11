"""VirtualReceiver over a test-double IQSource (S3 §2): generations, seek,
span bounds, bounded queue, RDS, processing cost."""

import asyncio
import time

import numpy as np
import pytest

from msdr.contracts.source import SourceDescriptor
from msdr.recorder.iq_stream import IQFrame
from msdr.source.base import SourceEvent
from msdr.source.virtual_receiver import OutOfSpanError, VirtualReceiver
from msdr.survey.rds import encode_rds_mpx, fm_modulate_chunks

_FS = 2_000_000
_CENTER = 100_000_000
_FRAME = 65_536


class _Stream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.enabled = False

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def enable(self, on: bool) -> None:
        self.enabled = on

    async def frames(self):
        while True:
            yield await self.queue.get()


class _Source:
    source_id = "replay:test"

    def __init__(self, fs: int = _FS, center: int = _CENTER) -> None:
        self.desc = SourceDescriptor(
            source_id=self.source_id, mode="replay", sample_rate_hz=fs, center_hz=center
        )
        self.listeners = []
        self.stream = _Stream()

    def descriptor(self) -> SourceDescriptor:
        return self.desc

    def iq_stream(self) -> _Stream:
        return self.stream

    def acquire_spectrum(self):
        return asyncio.Queue()

    def release_spectrum(self, q) -> None: ...

    def add_listener(self, cb) -> None:
        self.listeners.append(cb)

    def remove_listener(self, cb) -> None:
        self.listeners.remove(cb)

    def _emit(self, kind: str) -> None:
        ev = SourceEvent(kind, self.desc.generation, self.desc.sample_index, 0, self.desc)
        for cb in list(self.listeners):
            cb(ev)

    def seek(self, sample: int) -> None:
        self.desc = self.desc.bump("seek").model_copy(update={"sample_index": sample})
        self._emit("generation")

    def new_run(self, center: int) -> None:
        self.desc = self.desc.bump("retune").model_copy(
            update={"center_hz": center, "sample_index": 0}
        )
        self._emit("run")


def _nfm_iq(seconds: float, offset: float = 250e3, fs: int = _FS) -> bytes:
    t = np.arange(int(seconds * fs)) / fs
    z = 0.5 * np.exp(1j * (2 * np.pi * offset * t + 2.5 * np.sin(2 * np.pi * 1e3 * t)))
    return (np.column_stack([z.real, z.imag]) * 32767).astype("<i2").tobytes()


def _frames(iq: bytes, generation: int, start: int, size: int = _FRAME):
    step = size * 4
    return [
        IQFrame(iq[o : o + step], 0.0, generation=generation, sample_index=start + o // 4)
        for o in range(0, len(iq), step)
    ]


def test_native_listening_changes_do_not_change_decoder_tap():
    t = np.arange(400_000) / _FS
    z = 0.2 * np.exp(2j * np.pi * (250_000 + 1200) * t)
    iq = (np.column_stack([z.real, z.imag]) * 32767).astype("<i2").tobytes()
    taps = []
    audios = []
    for custom in (False, True):
        rx = VirtualReceiver(_Source())
        rx.tune(_CENTER + 250_000, "USB", 2700)
        if custom:
            rx.configure_audio(
                agc_mode="off", manual_gain=0.5, ssb_low_hz=500,
                notch_enabled=True, notch_frequency_hz=1200,
            )
        rx._clean_consumers = 1
        for frame in _frames(iq, 0, 0):
            rx.process_frame(frame)
        taps.append([rx._clean_queue.get_nowait()[0] for _ in range(rx._clean_queue.qsize())])
        audios.append([rx._queue.get_nowait().pcm for _ in range(rx._queue.qsize())])
    assert taps[0] == taps[1]
    assert audios[0] != audios[1]


def test_native_passband_rejects_width_that_cannot_fit_tune():
    rx = VirtualReceiver(_Source())
    rx.tune(_CENTER + 250_000, "USB", 2700)
    with pytest.raises(ValueError, match="retune wider"):
        rx.configure_audio(ssb_high_hz=3600)
    assert rx.status()["native_audio"]["ssb_high_hz"] is None
    rx.tune(_CENTER + 250_000, "USB", 6000)
    rx.configure_audio(ssb_low_hz=300, ssb_high_hz=3600)
    assert rx._chain.demod._band == (300.0, 3600.0)
    with pytest.raises(ValueError, match="passband"):
        rx.tune(_CENTER + 250_000, "USB", 2700)


def test_native_effect_state_resets_on_seek():
    iq = _nfm_iq(0.1)
    settings = dict(nr_enabled=True, nr_strength=0.7, notch_enabled=True)
    source = _Source()
    rx = VirtualReceiver(source)
    rx.configure_audio(**settings)
    rx.tune(_CENTER + 250_000, "NFM", 12_500)
    source.add_listener(rx._on_event)
    for frame in _frames(iq, 0, 0):
        rx.process_frame(frame)
    source.seek(0)
    assert rx._queue.empty()
    for frame in _frames(iq, 1, 0):
        rx.process_frame(frame)
    after_seek = [rx._queue.get_nowait().pcm for _ in range(rx._queue.qsize())]
    fresh = VirtualReceiver(_Source())
    fresh.configure_audio(**settings)
    fresh.tune(_CENTER + 250_000, "NFM", 12_500)
    for frame in _frames(iq, 0, 0):
        fresh.process_frame(frame)
    reference = [fresh._queue.get_nowait().pcm for _ in range(fresh._queue.qsize())]
    assert after_seek == reference


def test_out_of_span_tune_raises_with_span_reason():
    vr = VirtualReceiver(_Source())
    with pytest.raises(OutOfSpanError) as exc:
        vr.tune(_CENTER + 995_000, "NFM", 12_500)
    assert "span" in exc.value.reason
    assert "99.000000–101.000000 MHz" in exc.value.reason
    assert vr.status()["channel"] is None  # nothing changed
    st = vr.tune(_CENTER + 250_000, "nfm", 12_500)
    assert st["demodulator"] == "NFM" and st["state"] == "idle" and st["channel_generation"] == 1


async def test_seek_flushes_and_first_new_block_is_at_target():
    src = _Source()
    vr = VirtualReceiver(src)
    vr.tune(_CENTER + 250_000, "NFM", 12_500)
    await vr.start()
    iq = _nfm_iq(0.2)
    for f in _frames(iq, 0, 0):
        src.stream.queue.put_nowait(f)
    for _ in range(100):
        await asyncio.sleep(0)
        if vr._queue.qsize() >= 3:
            break
    assert vr._queue.qsize() >= 3
    target = 1_234_567
    src.seek(target)
    assert vr._queue.empty()  # flushed synchronously by the event
    # In-flight frames from before the seek, then the new generation.
    for f in _frames(iq, 0, 400_000)[:3] + _frames(iq, 1, target):
        src.stream.queue.put_nowait(f)
    blocks = []

    async def collect():
        async for b in vr.audio():
            blocks.append(b)
            if len(blocks) == 4:
                return

    await asyncio.wait_for(collect(), 5)
    assert all(b.generation == 1 for b in blocks)
    assert blocks[0].sample_index == target
    assert [b.sample_index for b in blocks] == [target + k * _FRAME for k in range(4)]
    st = vr.status()
    # the 3 in-flight frames plus any pre-seek frames still queued at the seek
    assert st["stale_frames"] >= 3 and st["generation"] == 1 and st["state"] == "running"
    await vr.stop()


def test_new_run_outside_span_goes_unavailable_then_recovers():
    src = _Source()
    vr = VirtualReceiver(src)
    src.add_listener(vr._on_event)
    vr.tune(_CENTER + 250_000, "NFM", 12_500)
    src.new_run(_CENTER + 5_000_000)
    st = vr.status()
    assert st["state"] == "unavailable" and "span" in st["reason"]
    for f in _frames(_nfm_iq(0.05), 1, 0):
        assert vr.process_frame(f) is None  # silent, no exception
    src.new_run(_CENTER)
    assert vr.status()["state"] != "unavailable"
    assert vr.process_frame(_frames(_nfm_iq(0.05), 2, 0)[0]) is not None


def test_dropped_frame_gap_resets_dsp_state_and_is_observable():
    # Finding C regression: ReplayIQStream drops a frame for a stalled
    # consumer without a discontinuity -- the next frame's sample_index just
    # jumps ahead, same generation. The receiver must detect that gap itself
    # and reset the demod chain (filter/decimator state is otherwise stale),
    # with the drop visible via a counter.
    src = _Source()
    vr = VirtualReceiver(src)
    vr.tune(_CENTER + 250_000, "NFM", 12_500)
    frames = _frames(_nfm_iq(0.3), 0, 0)
    assert vr.process_frame(frames[0]) is not None
    chain_before = vr._chain
    assert vr.status()["dropped_frame_gaps"] == 0

    # frames[1] is skipped -- as if a slow consumer's queue dropped it.
    assert vr.process_frame(frames[2]) is not None
    assert vr.status()["dropped_frame_gaps"] == 1
    assert vr._chain is not chain_before  # fresh DemodChain, not the stale one


def test_inspection_tracks_selected_channel_and_opt_in_carrier_correction():
    src = _Source(fs=250_000)
    vr = VirtualReceiver(src, queue_max=32)
    vr.tune(_CENTER, "CW", 1_000)
    vr.configure_drift(enabled=True, max_offset_hz=1_000, alpha=1.0, min_level_db=-50)
    fs = 250_000
    t = np.arange(fs) / fs
    # A 400 Hz residual carrier is inside the selected channel; the much
    # stronger 60 kHz component is outside it and must not dominate scope/drift.
    z = 0.25 * np.exp(2j * np.pi * 400 * t) + 0.75 * np.exp(2j * np.pi * 60_000 * t)
    data = (np.column_stack([z.real, z.imag]) * 32767).astype("<i2").tobytes()
    for frame in _frames(data, 0, 0, size=25_000):
        vr.process_frame(frame)
    reading = vr.drift_status()
    assert reading["accepted"] is True
    assert reading["offset_hz"] == pytest.approx(400, abs=3)
    assert reading["correction_enabled"] is True
    snapshot = vr.inspection.snapshot(window_s=0.05, points=256)
    assert snapshot["streams"]["iq_envelope"]["sample_rate_hz"] == 50_000
    assert snapshot["streams"]["iq_envelope"]["generation"] == 0
    assert snapshot["streams"]["iq_i"]["session_id"] == "replay:test:0:None:1"
    assert np.mean(snapshot["streams"]["iq_envelope"]["values"]) < 0.5


def test_bounded_queue_drops_oldest_and_counts():
    src = _Source()
    vr = VirtualReceiver(src, queue_max=4)
    vr.tune(_CENTER + 250_000, "NFM", 12_500)
    frames = _frames(_nfm_iq(0.33), 0, 0)
    for f in frames:
        vr.process_frame(f)
    assert vr.status()["dropped_blocks"] == len(frames) - 4
    assert vr._queue.qsize() == 4
    assert vr._queue.get_nowait().sample_index == (len(frames) - 4) * _FRAME


async def test_flat_tap_and_listening_controls_are_independent():
    src = _Source()
    vr = VirtualReceiver(src)
    vr.tune(_CENTER + 250_000, "NFM", 12_500)
    vr.mute(True)
    vr.volume(0.0)
    tap = vr.flat_tap()
    first = asyncio.ensure_future(tap.__anext__())
    await asyncio.sleep(0)  # let the generator register as a consumer
    block = None
    for f in _frames(_nfm_iq(0.1), 0, 0):
        block = vr.process_frame(f) or block
    pcm, gen, idx = await asyncio.wait_for(first, 1)
    assert gen == 0 and idx == 0
    flat = np.frombuffer(pcm, "<i2")
    assert np.abs(flat).max() > 10_000  # 2.5 kHz deviation ~ 0.5 FS, unmuted
    assert not np.frombuffer(block.pcm, "<i2").any()  # listening path muted
    await tap.aclose()


@pytest.mark.parametrize("pilot_level", [0.09, 0.0])
def test_wfm_rds_through_receiver(pilot_level):
    src = _Source()
    vr = VirtualReceiver(src)
    vr.tune(_CENTER + 400_000, "WFM", 200_000)
    mpx = encode_rds_mpx("TESTFM", 0x54A8, None, 2.5, 250_000, pilot_level=pilot_level)
    idx = 0
    for c in fm_modulate_chunks(mpx, 250_000, _FS, 400e3, 100_000, amplitude=0.3):
        data = (np.column_stack([c.real, c.imag]) * 32767).astype("<i2").tobytes()
        vr.process_frame(IQFrame(data, 0.0, generation=0, sample_index=idx))
        idx += c.size
    rds = vr.rds()
    assert rds["pi"] == "0x54A8" and rds["ps"] == "TESTFM"
    assert vr.status()["rds"]["ps"] == "TESTFM"


def test_processing_cost_10s_of_2msps_nfm():
    src = _Source()
    vr = VirtualReceiver(src, queue_max=1000)
    vr.tune(_CENTER + 250_000, "NFM", 12_500)
    vr.squelch(10.0)
    # 1 s holds whole cycles of the 250 kHz offset and the 1 kHz tone, so
    # repeating it is phase-continuous; frames of 125 000 samples (62.5 ms).
    one_s = _nfm_iq(1.0)
    frames = _frames(one_s, 0, 0, size=125_000)
    t0 = time.perf_counter()
    n = 0
    for rep in range(10):
        for f in frames:
            vr.process_frame(
                IQFrame(f.data, 0.0, generation=0, sample_index=rep * _FS + f.sample_index)
            )
            n += len(f.data) // 4
    wall = time.perf_counter() - t0
    print(f"\n10 s of 2 MS/s NFM processed in {wall:.3f} s wall (cpu_load {vr._cpu_load:.3f})")
    assert n == 10 * _FS
    assert vr.status()["squelch_open"] is True
    assert wall < 2.0
