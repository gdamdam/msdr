"""A signal probe preserves transient sync and never publishes its events."""

import pytest

from msdr.decoder import signal_test


class FakeBand:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.polls = 0
        self.stopped = False
        self.instances.append(self)

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    def status(self):
        self.polls += 1
        return {
            "iq": {"frames": self.polls},
            "channels": [
                {
                    "decoder_status": [
                        {
                            "id": "dmr",
                            "state": "running",
                            "synchronized": self.polls == 3,
                            "events": 0,
                            "valid_frames": 0,
                        }
                    ]
                }
            ],
        }


@pytest.mark.asyncio
async def test_transient_sync_is_preserved_and_probe_stops(monkeypatch):
    monkeypatch.setattr(signal_test, "resolve_binaries", lambda _: {"dsd-fme": "/fake/dsd-fme"})
    monkeypatch.setattr(signal_test, "_POLL_S", 0.005)
    result = await signal_test.test_signal(
        frequency_hz=451_100_000,
        center_hz=452_000_000,
        sample_rate_hz=4_000_000,
        requested=["dmr"],
        bin_dirs=[],
        iq_stream_factory=lambda: None,
        observe_s=0.03,
        band_factory=FakeBand,
    )
    assert result["outcome"] == "matched"
    assert result["results"][0]["state"] == "synchronized"
    band = FakeBand.instances[-1]
    assert band.polls > 1
    assert band.stopped
    assert band.kwargs["detect"] is False
    # The frame-sync identifier always rides along with the chosen decoders.
    assert band.kwargs["memories"] == [(451_100_000, "signal-test", ["dmr", "voicesync"])]
    assert result["frame_sync"] is None


def test_candidate_selection_limits_work_and_rejects_wideband():
    binaries = {"dsd-fme": "/fake/dsd-fme", "multimon-ng": None}
    selected, unavailable = signal_test.probe_decoders(
        451_100_000,
        ["adsb", "dmr", "nxdn96", "pocsag", "p25", "dstar"],
        binaries,
    )
    assert selected == ["dmr", "nxdn96", "p25"]
    assert unavailable == ["pocsag"]


class OffsetBand(FakeBand):
    sync_offsets = {2000}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.base_hz = kwargs["memories"][0][0]
        self.frequency_hz = self.base_hz

    async def retune(self, _center_hz, _sample_rate_hz, memories):
        self.frequency_hz = memories[0][0]

    def status(self):
        self.polls += 1
        return {
            "iq": {"frames": self.polls},
            "channels": [
                {
                    "decoder_status": [
                        {
                            "id": "dmr",
                            "state": "running",
                            "synchronized": self.frequency_hz - self.base_hz in self.sync_offsets,
                            "events": 0,
                            "valid_frames": 0,
                        }
                    ]
                }
            ],
        }


@pytest.mark.asyncio
async def test_optimizer_requires_repeatable_unique_sync(monkeypatch):
    monkeypatch.setattr(signal_test, "resolve_binaries", lambda _: {"dsd-fme": "/fake/dsd-fme"})
    monkeypatch.setattr(signal_test, "_POLL_S", 0.001)
    monkeypatch.setattr(signal_test, "_OPTIMIZE_OBSERVE_S", 0.005)
    kwargs = dict(
        frequency_hz=451_100_000,
        center_hz=452_000_000,
        sample_rate_hz=4_000_000,
        requested=["dmr"],
        bin_dirs=[],
        iq_stream_factory=lambda: None,
        optimize=True,
        band_factory=OffsetBand,
    )
    result = await signal_test.test_signal(**kwargs)
    assert result["recommended_hz"] == 451_102_000
    assert result["recommended_decoder"] == "dmr"
    assert OffsetBand.instances[-1].stopped

    monkeypatch.setattr(OffsetBand, "sync_offsets", {-2000, 2000})
    ambiguous = await signal_test.test_signal(**kwargs)
    assert ambiguous["outcome"] == "unchanged"
    assert ambiguous["recommended_hz"] is None


class SyncBand(FakeBand):
    """Reports a System Fusion frame sync through on_event, like the real band."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active = list(kwargs["memories"][0][2])
        self.retuned = []

    async def start(self):
        from msdr.decoder.backends import DecodeEvent

        self.kwargs["on_event"](
            DecodeEvent(
                ts=0.0,
                decoder="voicesync",
                kind="text",
                text="System Fusion frame sync seen",
                fields={"decoder_id": "ysf", "protocol": "System Fusion"},
            )
        )

    async def retune(self, _center_hz, _sample_rate_hz, memories):
        self.active = list(memories[0][2])
        self.retuned.append(memories)

    def status(self):
        self.polls += 1
        return {
            "iq": {"frames": self.polls},
            "channels": [
                {
                    "decoder_status": [
                        {
                            "id": decoder_id,
                            "state": "running",
                            "synchronized": decoder_id in {"voicesync", "ysf"},
                            "events": 0,
                            "valid_frames": 0,
                        }
                        for decoder_id in self.active
                    ]
                }
            ],
        }


async def _run_sync_band(monkeypatch, binaries, requested):
    monkeypatch.setattr(signal_test, "resolve_binaries", lambda _: binaries)
    monkeypatch.setattr(signal_test, "_POLL_S", 0.005)
    result = await signal_test.test_signal(
        frequency_hz=451_100_000,
        center_hz=452_000_000,
        sample_rate_hz=4_000_000,
        requested=requested,
        bin_dirs=[],
        iq_stream_factory=lambda: None,
        observe_s=0.03,
        band_factory=SyncBand,
    )
    return result, SyncBand.instances[-1]


@pytest.mark.asyncio
async def test_frame_sync_adds_the_decoder_that_was_not_tried(monkeypatch):
    result, band = await _run_sync_band(monkeypatch, {"dsd-fme": "/fake/dsd-fme"}, ["dmr"])
    assert result["frame_sync"]["decoder_id"] == "ysf"
    assert band.retuned == [[(451_100_000, "signal-test", ["ysf"])]]
    rows = {row["id"]: row for row in result["results"]}
    assert rows["voicesync"]["detail"] == "System Fusion frame sync seen"
    assert rows["ysf"]["state"] == "synchronized"
    assert rows["dmr"]["state"] == "inconclusive"
    assert "ysf" in band.kwargs["decoders"]


@pytest.mark.asyncio
async def test_frame_sync_names_the_system_without_its_decoder(monkeypatch):
    # No dsd-fme: the system is still identified, and its decoder is listed
    # as unavailable rather than silently skipped.
    result, band = await _run_sync_band(monkeypatch, {"dsd-fme": None}, ["dmr"])
    assert result["outcome"] == "matched"
    assert result["frame_sync"]["protocol"] == "System Fusion"
    assert band.retuned == []
    assert [row["id"] for row in result["results"]] == ["voicesync"]
    assert set(result["unavailable"]) == {"dmr", "ysf"}


class SilentBand(FakeBand):
    """Runs its decoder, never syncs, and feeds the span tap an empty span."""

    async def start(self):
        import numpy as np

        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(65_536) + 1j * rng.standard_normal(65_536)) * 0.002
        self.kwargs["span_stats"].update(noise.astype(np.complex64))

    def status(self):
        self.polls += 1
        return {
            "iq": {"frames": self.polls, "dropped_frame_gaps": 2},
            "channels": [
                {
                    "decoder_status": [
                        {"id": "dmr", "state": "running", "synchronized": False, "events": 0}
                    ]
                }
            ],
        }


@pytest.mark.asyncio
async def test_inconclusive_test_says_what_the_span_showed(monkeypatch):
    monkeypatch.setattr(signal_test, "resolve_binaries", lambda _: {"dsd-fme": "/fake/dsd-fme"})
    monkeypatch.setattr(signal_test, "_POLL_S", 0.005)
    result = await signal_test.test_signal(
        frequency_hz=451_100_000,
        center_hz=451_000_000,
        sample_rate_hz=2_000_000,
        requested=["dmr"],
        bin_dirs=[],
        iq_stream_factory=lambda: None,
        observe_s=0.03,
        band_factory=SilentBand,
    )
    assert result["outcome"] == "inconclusive"
    assert [d["code"] for d in result["diagnosis"]] == ["dropped_samples", "no_signal"]
