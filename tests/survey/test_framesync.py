"""Frame-sync search: every system is found from its sync at the frame period,
and nothing is claimed for signals that merely share the symbol rate."""

from __future__ import annotations

import numpy as np
import pytest

from msdr.decoder.registry import DECODERS
from msdr.survey import framesync as fs
from msdr.validation import fixtures as fx
from msdr.validation import impairments as imp
from msdr.validation.fixtures_framesync import RATE_HZ, framed_levels, fsk_iq, random_levels

_PATTERNS = {(p.decoder_id, p.name): (baud, p) for baud, ps in fs._BY_BAUD.items() for p in ps}

# The same syncs as dsd-fme writes them (one character per symbol, '1'
# positive, '3' negative): a second source for the constants' signs.
_DSD_SIGNS = {
    ("dmr", "BS voice sync"): "131111333113313313113313",
    ("dmr", "BS data sync"): "313333111331131131331131",
    ("dmr", "MS voice sync"): "133313311131311113313331",
    ("dmr", "MS data sync"): "311131133313133331131113",
    ("p25", "frame sync"): "111113113311333313133333",
    ("nxdn96", "frame sync word"): "3131331131",
    ("nxdn", "frame sync word"): "3131331131",
    ("ysf", "frame sync"): "31111311313113131131",
    ("m17", "stream sync"): "33331131",
    ("dpmr", "frame sync 2"): "113333131331",
}


def _iq(key, *, frames=None, seed=0, **kwargs):
    baud, pattern = _PATTERNS[key]
    # About 1.2 s of signal, and never fewer than 6 frames.
    frames = frames or max(6, int(1.2 * baud / pattern.period))
    levels = framed_levels(
        pattern.levels, pattern.period, frames, seed=seed, two_level=pattern.two_level
    )
    return fsk_iq(levels, baud=baud, **kwargs)


@pytest.mark.parametrize("key", sorted(_DSD_SIGNS))
def test_sync_constants_agree_with_dsd_fme_signs(key):
    _, pattern = _PATTERNS[key]
    signs = "".join("1" if level > 0 else "3" for level in pattern.levels)
    assert signs == _DSD_SIGNS[key]


def test_every_pattern_names_a_registered_decoder():
    for _, pattern in _PATTERNS.values():
        assert pattern.decoder_id in DECODERS


@pytest.mark.parametrize("key", sorted(_PATTERNS))
def test_each_system_is_found_from_its_repeating_sync(key):
    baud, pattern = _PATTERNS[key]
    found = fs.detect_frame_sync(_iq(key), RATE_HZ)
    assert found, f"nothing found for {key}"
    top = found[0]
    assert (top.decoder_id, top.symbol_rate_hz) == (pattern.decoder_id, baud)
    assert top.inverted is False
    assert top.symbol_errors <= 1
    # Only the system that was sent is reported.
    assert {item.decoder_id for item in found} == {pattern.decoder_id}


def test_inverted_polarity_is_found_and_flagged():
    found = fs.detect_frame_sync(np.conj(_iq(("p25", "frame sync"))), RATE_HZ)
    assert found[0].decoder_id == "p25"
    assert found[0].inverted is True


def test_survives_noise_and_a_tuning_offset():
    iq = _iq(("dmr", "BS voice sync"), offset_hz=600.0)
    found = fs.detect_frame_sync(imp.awgn(iq, 15.0, seed=4), RATE_HZ)
    assert found and found[0].decoder_id == "dmr"


def test_burst_between_silence_is_found():
    # A carrier that keys up and down inside the capture: the gaps must not
    # set the level scale or count as symbols.
    burst = _iq(("ysf", "frame sync"))
    gap = np.zeros(RATE_HZ // 5, dtype=np.complex64)
    iq = imp.awgn(np.concatenate([gap, burst, gap]), 25.0, seed=2)
    found = fs.detect_frame_sync(iq, RATE_HZ)
    assert found and found[0].decoder_id == "ysf"


def test_a_single_sync_is_not_enough():
    _, pattern = _PATTERNS[("p25", "frame sync")]
    levels = random_levels(4800, seed=3)
    levels[100 : 100 + len(pattern.levels)] = pattern.levels
    assert fs.detect_frame_sync(fsk_iq(levels, baud=4800), RATE_HZ) == []


@pytest.mark.parametrize("seed", range(4))
def test_random_four_level_data_claims_nothing(seed):
    for baud in (4800, 2400):
        iq = fsk_iq(random_levels(int(2.0 * baud), seed=seed), baud=baud)
        assert fs.detect_frame_sync(iq, RATE_HZ) == []


@pytest.mark.parametrize("seed", range(4))
def test_random_two_level_data_claims_nothing(seed):
    # e.g. an RS41 or other 4800 Bd GFSK stream: sliced to four levels it
    # sits on the outer ones, where the outer-only syncs would match by chance.
    for baud in (4800, 2400):
        iq = fsk_iq(random_levels(int(2.0 * baud), seed=seed, two_level=True), baud=baud)
        assert fs.detect_frame_sync(iq, RATE_HZ) == []


def test_noise_and_analogue_voice_claim_nothing():
    assert fs.detect_frame_sync(fx.white_noise_iq(1.0, RATE_HZ, seed=1), RATE_HZ) == []
    assert fs.detect_frame_sync(fx.nfm_voice_like_iq(), fx.IQ_RATE_HZ) == []


def test_unusable_input_returns_nothing():
    iq = _iq(("dmr", "BS voice sync"))
    assert fs.detect_frame_sync(iq, 9_600.0) == []  # too few samples per symbol
    assert fs.detect_frame_sync(iq[:200], RATE_HZ) == []  # too short
    assert fs.detect_frame_sync(np.zeros(RATE_HZ, np.complex64), RATE_HZ) == []
    assert fs.detect_frame_sync(np.full(RATE_HZ, np.nan + 0j), RATE_HZ) == []
