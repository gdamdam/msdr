import asyncio

import numpy as np

from msdr.decoder.pydecoders.dsc import _PHASING, DSCBackend, _Frame
from msdr.decoder.pydecoders.dsc_message import parse_dsc_symbols


def _word(symbol):
    # Inverse of DSC's 10-bit parity/data conversion.
    value = symbol << 1
    value = ((value & 0xF0) >> 4) | ((value & 0x0F) << 4)
    value = ((value & 0xCC) >> 2) | ((value & 0x33) << 2)
    value = ((value & 0xAA) >> 1) | ((value & 0x55) << 1)
    return (value << 3) | (7 - symbol.bit_count())


def _wire_symbols(symbols):
    # Offset-zero phasing sequence: detector locks at the final bit.
    sync = next(bits for bits, offset in _PHASING if offset == 0)
    eos = len(symbols) - 2
    wire = [symbols[0], 106, symbols[1], 105, symbols[2]]
    for i in range(eos):
        wire.append(symbols[i])  # RX copy
        wire.append(symbols[i + 3] if i + 3 < len(symbols) else symbols[-1])  # next DX
    wire.extend((symbols[eos], symbols[-1], symbols[-1]))
    stream = f"{sync:030b}" + "".join(f"{_word(s):010b}" for s in wire)
    return [int(bit) for bit in stream]


def _encoded(symbols):
    symbols = list(symbols)
    symbols[-1] = 0
    symbols[-1] = __import__("functools").reduce(int.__xor__, symbols[1:-1], 0)
    return symbols


def _iq(symbols, *, vhf):
    rate, bit_rate = (48000 if vhf else 12000), (1200 if vhf else 100)
    samples_per_bit = rate / bit_rate
    bits = _wire_symbols(symbols)
    counts = [
        round((i + 1) * samples_per_bit) - round(i * samples_per_bit) for i in range(len(bits))
    ]
    frequencies = []
    for bit, count in zip(bits, counts, strict=True):
        if vhf:
            frequencies.extend([2100.0 if bit else 1300.0] * count)
        else:
            frequencies.extend([85.0 if bit else -85.0] * count)
    freq = np.asarray(frequencies)
    if vhf:
        audio_phase = 2 * np.pi * np.cumsum(freq) / rate
        audio = np.sin(audio_phase)
        # FM's instantaneous RF frequency follows the 1300/2100-Hz audio tone.
        iq_phase = 2 * np.pi * 800 * np.cumsum(audio) / rate
        iq = np.exp(1j * iq_phase)
    else:
        iq = np.exp(2j * np.pi * np.cumsum(freq) / rate)
    scale = np.clip(np.round(np.column_stack((iq.real, iq.imag)) * 30000), -32768, 32767).astype(
        "<i2"
    )
    return scale.tobytes()


def _symbols_distress():
    # MMSI 123456789 encoded in 5 2-digit symbols, position 37N 122W.
    msg = [112, 12, 34, 56, 78, 90, 100, 13, 70, 1, 22, 20, 30, 0, 100, 127, 0]
    return _encoded(msg)


def test_semantic_parser_requires_ecc_and_valid_coordinates():
    message = _symbols_distress()
    fields = parse_dsc_symbols(message, ecc_ok=True)
    assert fields["sender_mmsi"] == "123456789"
    assert fields["latitude"] == 37.0
    assert fields["longitude"] == -122 - 20 / 60
    assert "latitude" not in parse_dsc_symbols(message, ecc_ok=False)
    malformed = message.copy()
    malformed[7:12] = [99, 99, 99, 99, 99]
    # Recompute ECC: semantic range checking still rejects the position.
    malformed[-1] = __import__("functools").reduce(int.__xor__, malformed[1:-1], 0)
    assert "latitude" not in parse_dsc_symbols(malformed, ecc_ok=True)
    malformed = message.copy()
    malformed[7:12] = [19, 00, 10, 00, 00]  # 90 degrees with nonzero minutes.
    malformed[-1] = __import__("functools").reduce(int.__xor__, malformed[1:-1], 0)
    assert "latitude" not in parse_dsc_symbols(malformed, ecc_ok=True)


def test_all_ships_group_and_selective_fields_are_bounded_to_supported_calls():
    all_ships = _encoded([116, 108, 12, 34, 56, 78, 90, 109, 126, 127, 0])
    fields = parse_dsc_symbols(all_ships, ecc_ok=True)
    assert fields["sender_mmsi"] == "123456789"
    assert fields["category"] == "safety"
    assert "recipient_mmsi" not in fields

    group = _encoded([114, 0, 00, 00, 00, 00, 100, 12, 34, 56, 78, 90, 100, 100, 127, 0])
    fields = parse_dsc_symbols(group, ecc_ok=True)
    assert fields["group_mmsi"] == "000000000"
    assert fields["sender_mmsi"] == "123456789"

    extended = all_ships.copy()
    extended[6] = 91  # nonzero tenth digit is an equipment suffix, not MMSI
    extended[-1] = __import__("functools").reduce(int.__xor__, extended[1:-1], 0)
    assert "sender_mmsi" not in parse_dsc_symbols(extended, ecc_ok=True)

    unsupported = _encoded([120, 12, 34, 56, 78, 90, 112, 12, 34, 56, 78, 90, 110, 127, 0])
    fields = parse_dsc_symbols(unsupported, ecc_ok=True)
    assert "recipient_mmsi" not in fields
    assert "sender_mmsi" not in fields


def test_hf_and_vhf_modems_emit_valid_distress_message():
    symbols = _symbols_distress()
    for vhf in (False, True):
        rate = 48000 if vhf else 12000
        backend = DSCBackend(
            None,
            sample_rate_hz=rate,
            center_hz=156500000 if vhf else 7000000,
            frequency_hz=156500000 if vhf else 7000000,
        )
        asyncio.run(backend.start())
        backend.feed(_iq(symbols, vhf=vhf))
        event = asyncio.run(asyncio.wait_for(backend.get_event(), timeout=0.25))
        assert event is not None, f"no {('VHF' if vhf else 'HF')} DSC frame"
        assert event.fields["ecc_ok"] is True
        assert event.fields["sender_mmsi"] == "123456789"
        assert event.fields["latitude"] == 37.0
        assert event.fields["longitude"] == -122 - 20 / 60
        assert "distress alert" in event.text
        assert "123456789" in event.text


def test_corrupt_ecc_and_truncated_input_never_assert_identity_or_position():
    symbols = _symbols_distress()
    symbols[-1] ^= 1
    assert not parse_dsc_symbols(symbols, ecc_ok=False)

    corrupted = _symbols_distress()
    corrupted[2] ^= 1  # Alter the decoded sender while leaving its ECC unchanged.
    backend = DSCBackend(None, sample_rate_hz=12000, center_hz=7000000, frequency_hz=7000000)
    asyncio.run(backend.start())
    backend.feed(_iq(corrupted, vhf=False))
    event = asyncio.run(asyncio.wait_for(backend.get_event(), timeout=0.25))
    assert event.fields["ecc_ok"] is False
    assert "sender_mmsi" not in event.fields
    assert "latitude" not in event.fields

    backend = DSCBackend(None, sample_rate_hz=12000, center_hz=7000000, frequency_hz=7000000)
    asyncio.run(backend.start())
    backend.feed(_iq(_symbols_distress(), vhf=False)[:40])
    assert backend._events.empty()


def test_position_request_and_ack_semantics_are_ecc_gated():
    prefix = [120, 12, 34, 56, 78, 90, 108, 11, 12, 22, 23, 30, 121, 126]
    request = _encoded(prefix + [126] * 6 + [117, 0])
    fields = parse_dsc_symbols(request, ecc_ok=True)
    assert fields["recipient_mmsi"] == "123456789"
    assert fields["sender_mmsi"] == "111222233"
    assert fields["position_request"] is True
    assert "latitude" not in fields
    assert "position_request" not in parse_dsc_symbols(request, ecc_ok=False)

    agreed = _encoded(prefix + [13, 70, 1, 22, 20, 126, 12, 34, 122, 0])
    fields = parse_dsc_symbols(agreed, ecc_ok=True)
    assert fields["position_acknowledgement"] == "agreed"
    assert fields["latitude"] == 37.0
    assert fields["longitude"] == -122 - 20 / 60
    assert fields["utc_time"] == "12:34"

    declined = _encoded(prefix + [126] * 6 + [12, 34, 122, 0])
    fields = parse_dsc_symbols(declined, ecc_ok=True)
    assert fields["position_acknowledgement"] == "declined"
    assert "latitude" not in fields
    assert fields["utc_time"] == "12:34"


def test_phasing_acquisition_recovers_one_flipped_phasing_bit():
    pattern = next(bits for bits, offset in _PHASING if offset == 0)
    bits = [int(bit) for bit in f"{pattern:030b}"]
    bits[11] ^= 1
    frame = _Frame()
    for bit in bits:
        frame.bit(bit)
    assert frame.phased
    assert frame.phase == 0
    assert frame.errors == 1
