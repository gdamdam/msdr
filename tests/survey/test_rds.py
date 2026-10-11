"""RDS decoder: synthetic encoder -> FM IQ at 8 MS/s -> WfmChannel/RdsDemod
-> RdsParser, plus bit-level block-sync/correction checks."""


import numpy as np

from msdr.survey.rds import (
    OFFSET_WORDS,
    RdsDemod,
    RdsGroup,
    RdsParser,
    RdsReceiver,
    block_word,
    encode_rds_bits,
    encode_rds_mpx,
    fm_channel_hz,
    fm_modulate_chunks,
    rbds_callsign,
    syndrome,
)

_FS = 8_000_000
_MPX_FS = 250_000
_PS = "TESTFM"
_PI = 0x54A8
_RT = "Hello RDS world"


def _decode(
    seconds: float,
    snr_db: float | None = None,
    offset_hz: float = 1_300_000,
    pilot_level: float = 0.09,
    receiver: list | None = None,
) -> dict:
    mpx = encode_rds_mpx(_PS, _PI, _RT, seconds, _MPX_FS, pilot_level=pilot_level)
    # Tuned 3 kHz off, like a label centroid that isn't exactly on the carrier.
    rx = RdsReceiver(_FS, offset_hz + 3_000)
    if receiver is not None:
        receiver.append(rx)
    rng = np.random.default_rng(7)
    amplitude = 0.3
    for chunk in fm_modulate_chunks(mpx, _MPX_FS, _FS, offset_hz, 400_000, amplitude=amplitude):
        if snr_db is not None:
            # SNR measured in a 200 kHz channel around the station.
            noise_total = amplitude**2 / 10 ** (snr_db / 10) * (_FS / 200e3)
            sigma = np.float32(np.sqrt(noise_total / 2))
            noise = rng.normal(size=chunk.size) + 1j * rng.normal(size=chunk.size)
            chunk = chunk + noise.astype(np.complex64) * sigma
        rx.process(chunk)
    return rx.parser.result()


def test_decodes_ps_pi_rt_from_clean_iq():
    result = _decode(2.5)
    assert result["ps"] == _PS
    assert result["pi"] == "0x54A8"
    assert result["rt"] == _RT
    assert result["pty"] == 10
    assert result["pty_name"] == "Country"  # RBDS table by default
    assert result["tp"] is False
    assert result["block_error_rate"] == 0.0
    assert result["groups"] > 10


def test_mono_station_without_pilot_decodes():
    # Live 2026-09-26: KNBR 104.5 (mono sports) carries RDS but no 19 kHz
    # pilot; the pilot-locked demod decoded nothing from it.
    rx: list = []
    result = _decode(3.0, snr_db=25.0, pilot_level=0.0, receiver=rx)
    assert result["ps"] == _PS
    assert result["pi"] == "0x54A8"
    assert rx[0].demod.pilot_locked is False


def test_stereo_station_stays_pilot_locked():
    rx: list = []
    assert _decode(2.5, receiver=rx)["ps"] == _PS
    assert rx[0].demod.pilot_locked is True


def test_decodes_at_moderate_snr():
    result = _decode(3.0, snr_db=20.0, offset_hz=-2_100_000)
    assert result["ps"] == _PS
    assert result["pi"] == "0x54A8"


def test_noise_only_produces_nothing():
    rx = RdsReceiver(_FS, 500_000)
    rng = np.random.default_rng(3)
    for _ in range(20):
        noise = (rng.normal(size=400_000) + 1j * rng.normal(size=400_000)) * 0.05
        rx.process(noise.astype(np.complex64))
    result = rx.parser.result()
    assert result["ps"] is None
    assert result["pi"] is None


def test_block_code_offsets_are_syndromes():
    for name, offset in OFFSET_WORDS.items():
        assert syndrome(block_word(0x1234, name)) == offset


def _feed(bits: np.ndarray) -> RdsParser:
    demod = RdsDemod(250_000)
    parser = RdsParser()
    for group in demod.feed_bits(bits.tolist()):
        parser.feed(group)
    parser.add_block_stats(*demod.pop_block_stats())
    return parser


def test_single_bit_errors_are_corrected():
    bits = encode_rds_bits(_PS, _PI, None, 40)
    rng = np.random.default_rng(1)
    # One flipped bit in every block after the first four (sync) blocks.
    for block in range(4, bits.size // 26):
        bits[block * 26 + rng.integers(0, 26)] ^= 1
    parser = _feed(bits)
    assert parser.ps == _PS
    assert parser.pi == _PI
    assert parser.block_errors == 0


def test_heavy_burst_errors_never_yield_wrong_ps():
    rng = np.random.default_rng(2)
    for trial in range(20):
        bits = encode_rds_bits(_PS, _PI, None, 60)
        for block in range(4, bits.size // 26):
            if rng.random() < 0.5:
                start = block * 26 + rng.integers(0, 22)
                # 3-5 random bit errors: beyond the 2-bit burst corrector.
                for pos in rng.choice(np.arange(start, start + 5), size=rng.integers(3, 6)):
                    bits[pos] ^= 1
        ps = _feed(bits).ps
        assert ps in (None, _PS), f"trial {trial}: wrong PS {ps!r}"


def test_rds_pty_table_option():
    parser = RdsParser(pty_table="rds")
    bits = encode_rds_bits(_PS, _PI, None, 12, pty=10)
    demod = RdsDemod(250_000)
    for group in demod.feed_bits(bits.tolist()):
        parser.feed(group)
    assert parser.result()["pty_name"] == "Pop Music"


# -- real broadcast excerpts ---------------------------------------------------------
# MPX (1.0 == 75 kHz deviation, float16 to keep the files small) out of
# WfmChannel on a real 8 MS/s capture of the US FM band.



def _decode_mpx(mpx: np.ndarray, chunk: int = 12_500) -> RdsParser:
    demod = RdsDemod(_MPX_FS)
    parser = RdsParser()
    for start in range(0, mpx.size, chunk):
        for group in demod.process(mpx[start : start + chunk]):
            parser.feed(group)
        parser.add_block_stats(*demod.pop_block_stats())
    return parser


# -- robustness ------------------------------------------------------------------------


def test_recovers_quickly_after_input_gaps():
    # Dropped IQ frames: 50 ms holes in the MPX (the demod can't know).
    mpx = encode_rds_mpx(_PS, _PI, _RT, 4.0, _MPX_FS).astype(np.float32)
    chunk = 12_500
    kept = [mpx[s : s + chunk] for i, s in enumerate(range(0, mpx.size, chunk)) if i % 10 != 5]
    parser = _decode_mpx(np.concatenate(kept), chunk)
    # 3.6 s of signal ~ 41 groups; each hole may cost the group around it.
    assert parser.groups >= 34
    assert parser.block_errors / parser.blocks < 0.15
    assert parser.ps == _PS


def test_soft_decision_corrects_two_separate_symbol_errors():
    bits = encode_rds_bits(_PS, _PI, None, 40)
    rel = np.full(bits.size, 1.0)
    for block in range(4, bits.size // 26):
        # Two wrong symbols far apart in the block: 2+2 bit errors, beyond
        # the 2-bit burst corrector, but flagged as the least reliable.
        for sym in (block * 26 + 3, block * 26 + 17):
            bits[sym] ^= 1
            bits[sym + 1] ^= 1
            rel[sym] = 0.1
    hard = _feed(bits.copy())
    assert hard.ps is None
    demod = RdsDemod(250_000)
    parser = RdsParser()
    for group in demod.feed_bits(bits.tolist(), rel.tolist()):
        parser.feed(group)
    parser.add_block_stats(*demod.pop_block_stats())
    assert parser.ps == _PS
    assert parser.block_errors == 0


def test_dynamic_ps_pages_never_mix():
    parser = RdsParser()

    def ps_group(text: str, seg: int) -> RdsGroup:
        word = (ord(text[2 * seg]) << 8) | ord(text[2 * seg + 1])
        return RdsGroup([_PI, (10 << 5) | seg, 0xE0CD, word], False)

    for _ in range(2):
        for seg in range(4):
            parser.feed(ps_group("PAGE-ONE", seg))
    assert parser.ps == "PAGE-ONE"
    for _ in range(2):
        for seg in range(2):
            parser.feed(ps_group("NEXTPAGE", seg))
    assert parser.ps == "PAGE-ONE"  # never "NEXT-ONE"
    for _ in range(2):
        for seg in range(4):
            parser.feed(ps_group("NEXTPAGE", seg))
    assert parser.ps == "NEXTPAGE"


def test_rbds_callsign_from_pi():
    assert rbds_callsign(0x1464) == "KBRG"  # 100.3 SF, live 2026-09-26
    assert rbds_callsign(0x3D0E) == "KRBQ"
    assert rbds_callsign(0x1000) == "KAAA"
    assert rbds_callsign(0x54A8) == "WAAA"
    assert rbds_callsign(0xA4FF) == "KSOL"  # Axyz compressed form of 0x40FF
    assert rbds_callsign(0xAF12) == rbds_callsign(0x1200)
    assert rbds_callsign(0x0123) is None
    assert rbds_callsign(0xC3A1) is None  # not a US call-letter PI
    assert rbds_callsign(None) is None


def test_parser_result_has_callsign_only_with_rbds_table():
    rbds, rds = RdsParser(), RdsParser(pty_table="rds")
    rbds.pi = rds.pi = 0x1464
    assert rbds.result()["callsign"] == "KBRG"
    assert rds.result()["callsign"] is None


def test_fm_channel_hz_odd_tenths_grid():
    assert fm_channel_hz(102_240_000) == 102_200_000
    assert fm_channel_hz(102_240_000, odd_tenths=True) == 102_300_000
    assert fm_channel_hz(102_190_000, odd_tenths=True) == 102_100_000
    assert fm_channel_hz(88_100_000, odd_tenths=True) == 88_100_000
    assert fm_channel_hz(107_950_000, odd_tenths=True) == 107_900_000
