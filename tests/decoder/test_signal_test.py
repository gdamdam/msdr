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
    assert band.kwargs["memories"] == [(451_100_000, "signal-test", ["dmr"])]


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
