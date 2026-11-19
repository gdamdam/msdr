"""Digital voice identify: the PyDecoder names a system from its frame sync,
once, and claims nothing it did not decode."""

from __future__ import annotations

import numpy as np

from msdr.decoder.pydecoders.voicesync import VoiceSyncDecoder
from msdr.survey import framesync as fs
from msdr.validation.fixtures_framesync import RATE_HZ, framed_levels, fsk_iq, random_levels


def _disc(iq: np.ndarray) -> np.ndarray:
    return np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)


def _dmr_disc(seconds: float = 4.0) -> np.ndarray:
    pattern = next(p for p in fs._BY_BAUD[4800] if p.name == "BS voice sync")
    frames = int(seconds * 4800 / pattern.period)
    return _disc(fsk_iq(framed_levels(pattern.levels, pattern.period, frames), baud=4800))


def test_names_the_system_once_and_sets_sync():
    decoder = VoiceSyncDecoder()
    assert decoder.input_rate == RATE_HZ
    disc = _dmr_disc()
    events = []
    for chunk in np.array_split(disc, 40):  # ~100 ms batches, as PythonBackend feeds
        events.extend(decoder.feed(chunk))
    events.extend(decoder.flush())
    assert decoder.synchronized is True
    assert len(events) == 1  # several windows saw it; one message
    event = events[0]
    assert event.decoder == "voicesync"
    assert event.fields["decoder_id"] == "dmr"
    assert event.fields["protocol"] == "DMR"
    assert "nothing decoded" in event.text
    assert event.integrity == "unknown"  # a sync is not a checked frame


def test_random_data_and_noise_stay_silent():
    decoder = VoiceSyncDecoder()
    random = _disc(fsk_iq(random_levels(3 * 4800, seed=5), baud=4800))
    noise = np.random.default_rng(1).uniform(-np.pi, np.pi, 3 * RATE_HZ).astype(np.float32)
    assert decoder.feed(random) + decoder.feed(noise) + decoder.flush() == []
    assert decoder.synchronized is False


def test_carrier_between_no_carrier_noise_is_found():
    # Audio path only: with no carrier the discriminator is loud noise, which
    # must not set the level scale for the signal next to it.
    rng = np.random.default_rng(2)
    noise = rng.uniform(-np.pi, np.pi, RATE_HZ // 4).astype(np.float32)
    disc = np.concatenate([noise, _dmr_disc(1.4), noise])
    found = fs.detect_frame_sync_disc(fs.quiet_only(disc, RATE_HZ), RATE_HZ)
    assert found and found[0].decoder_id == "dmr"
