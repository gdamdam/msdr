"""AcarsDecoder on a synthetic MSK-on-AM generator.

SYNTHETIC ONLY: the generator is written from the same reading of ARINC 618 as
the decoder (NRZI, tone assignment), so a round trip proves self-consistency,
not interoperability with real aircraft. No off-air validation."""

import numpy as np
import pytest

from msdr.decoder.pydecoders.acars import AcarsDecoder, crc16_kermit

FS = 22050
BAUD = 2400


def _with_parity(c: int) -> int:
    return c | 0x80 if bin(c).count("1") % 2 == 0 else c


def build_frame(
    reg="N12345", ack="A", label="H1", block="2", text="M01AUA1234HELLO", mode="2",
    etb=False, bad_crc=False,
) -> bytes:
    chars = [ord(mode), *reg.rjust(7, ".").encode(), ord(ack), *label.encode(), ord(block)]
    if text:
        chars += [0x02, *text.encode()]
    chars.append(0x17 if etb else 0x03)
    body = bytes(_with_parity(c) for c in chars)
    crc = crc16_kermit(body)
    if bad_crc:
        crc ^= 0x0101  # CRC bytes carry no parity, so the frame stays parity-clean
    return (
        bytes([0x2B | 0x80, 0x2A, 0x16, 0x16, 0x01])
        + body
        + bytes([crc & 0xFF, crc >> 8, 0x7F])
    )


def modulate(frame: bytes, snr_db: float | None, rng, lead_bits=40, invert=False) -> np.ndarray:
    bits = [1] * lead_bits
    for byte in frame:
        bits += [(byte >> i) & 1 for i in range(8)]  # LSB first
    bits += [1] * 16
    tone, tones = 1, []
    for b in bits:
        if (b == 0) != invert:
            tone ^= 1
        tones.append(2400.0 if tone else 1200.0)
    spb = FS / BAUD
    n = int(len(bits) * spb)
    freq = np.array(tones)[np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)]
    sig = np.sin(2 * np.pi * np.cumsum(freq) / FS)
    # AM audio: a little amplitude ripple plus lead-in/out silence of unequal length.
    sig *= 0.6 * (1 + 0.1 * np.sin(2 * np.pi * 3 * np.arange(n) / FS))
    pad = rng.integers(500, 900)
    sig = np.concatenate([np.zeros(pad), sig, np.zeros(2000)])
    if snr_db is not None:
        sigma = np.sqrt(np.mean(sig[pad : pad + n] ** 2) / 10 ** (snr_db / 10))
        sig = sig + rng.normal(0, sigma, sig.size)
    return sig.astype(np.float32)


def run(sig: np.ndarray, chunk: int = 4410):
    dec = AcarsDecoder()
    events = []
    for i in range(0, sig.size, chunk):
        events += dec.feed(sig[i : i + chunk])
    return events + dec.flush()


def test_crc_known_vector():
    # CRC-16/KERMIT check value for "123456789" is 0x2189.
    assert crc16_kermit(b"123456789") == 0x2189


def test_clean_round_trip_downlink_with_flight_id():
    ev = run(modulate(build_frame(), None, np.random.default_rng(1)))
    assert len(ev) == 1
    f = ev[0].fields
    assert ev[0].decoder == "acars" and ev[0].integrity == "passed"
    assert f["registration"] == "N12345" and f["label"] == "H1"
    assert f["flight_id"] == "UA1234" and f["message_no"] == "01A"
    assert f["block_id"] == "2" and f["mode"] == "2" and f["crc_ok"] is True
    assert ev[0].text == "HELLO"


@pytest.mark.parametrize("seed", range(5))
def test_round_trip_moderate_snr(seed):
    rng = np.random.default_rng(seed)
    ev = run(modulate(build_frame(reg="G-ABCD", text="X" * 60), 10.0, rng))
    assert [e.fields["registration"] for e in ev] == ["G-ABCD"]
    assert ev[0].fields["crc_ok"] is True


def test_ack_frame_without_text_and_etb():
    ack = build_frame(text="", label="_\x7f", ack="\x15")
    ev = run(modulate(ack, None, np.random.default_rng(2)))
    assert len(ev) == 1 and ev[0].fields["label"] == "_d" and ev[0].fields["ack"] == "NAK"
    ev = run(modulate(build_frame(etb=True), None, np.random.default_rng(3)))
    assert ev[0].fields["more_blocks"] is True


def test_bad_crc_is_flagged_not_passed():
    ev = run(modulate(build_frame(bad_crc=True), None, np.random.default_rng(4)))
    assert len(ev) == 1
    assert ev[0].fields["crc_ok"] is False and ev[0].integrity == "failed"


def test_body_parity_error_is_dropped():
    frame = bytearray(build_frame())
    frame[8] ^= 0x01  # flip one bit inside the text -> parity violation
    assert run(modulate(bytes(frame), None, np.random.default_rng(5))) == []


def test_noise_only_emits_nothing():
    rng = np.random.default_rng(6)
    assert run(rng.normal(0, 0.3, FS * 20).astype(np.float32)) == []


def test_two_frames_back_to_back_in_odd_chunks():
    rng = np.random.default_rng(7)
    sig = np.concatenate(
        [modulate(build_frame(reg="N1"), 12.0, rng), modulate(build_frame(reg="N2"), 12.0, rng)]
    )
    ev = run(sig, chunk=1001)
    assert [e.fields["registration"] for e in ev] == ["N1", "N2"]


def test_inverted_nrzi_polarity_still_decodes():
    # Opposite NRZI reading: the tone changes on 1 instead of 0.
    ev = run(modulate(build_frame(), None, np.random.default_rng(8), invert=True))
    assert len(ev) == 1 and ev[0].fields["crc_ok"] is True
    assert ev[0].fields["registration"] == "N12345"
