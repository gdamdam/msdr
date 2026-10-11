#!/usr/bin/env python3
"""End-to-end synthetic gr-lora_sdr TX -> msdr RX smoke check.

Run with the same GNU Radio 3.10/gr-lora_sdr environment used by msdr:
    python scripts/validate_lora_iq.py
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pmt
from gnuradio import blocks, gr
from gnuradio.lora_sdr.lora_sdr_lora_tx import lora_sdr_lora_tx

from msdr.decoder.pydecoders.lora import LoRaBackend
from msdr.decoder.registry import DECODERS


async def main() -> None:
    payload = b"msdr synthetic LoRa IQ validation"
    tx = lora_sdr_lora_tx(
        bw=250_000,
        cr=1,
        has_crc=True,
        impl_head=False,
        samp_rate=250_000,
        sf=7,
        frame_zero_padd=128,
        sync_word=[0x12],
    )
    sink = blocks.vector_sink_c()
    tx_graph = gr.top_block("msdr LoRa synthetic TX")
    tx_graph.connect(tx, sink)
    tx_graph.start()
    message = pmt.intern(payload.decode("ascii"))
    # The hierarchy routes its input to whitening. Its Python wrapper has no
    # message queue, and whitening expects a PMT symbol rather than a PDU.
    tx.lora_sdr_whitening_0.to_basic_block()._post(pmt.intern("msg"), message)

    deadline = time.monotonic() + 5
    while len(sink.data()) < 4096 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    tx_graph.stop()
    tx_graph.wait()
    samples = np.asarray(sink.data(), dtype=np.complex64)
    if len(samples) < 4096:
        raise RuntimeError(f"TX generated too few samples: {len(samples)}")

    # Give the synchronizer a quiet lead-in and tail, then quantize to the exact
    # little-endian interleaved int16 SDRconnect input format used by msdr.
    samples = np.concatenate(
        [np.zeros(1024, np.complex64), samples, np.zeros(4096, np.complex64)]
    )
    iq = np.empty((len(samples), 2), dtype="<i2")
    iq[:, 0] = np.clip(samples.real * 30000, -32768, 32767).astype("<i2")
    iq[:, 1] = np.clip(samples.imag * 30000, -32768, 32767).astype("<i2")

    backend = LoRaBackend(
        DECODERS["lora"],
        sample_rate_hz=250_000,
        center_hz=915_000_000,
        frequency_hz=915_000_000,
        spreading_factor=7,
        bandwidth_hz=250_000,
        coding_rate=1,
        protocol="lora",
    )
    await backend.start()
    try:
        events = backend.events()
        raw = iq.tobytes()
        for offset in range(0, len(raw), 4096 * 4):
            backend.feed(raw[offset : offset + 4096 * 4])
        event = await asyncio.wait_for(anext(events), timeout=8)
        if event.fields.get("payload_hex") != payload.hex():
            raise AssertionError(f"decoded payload mismatch: {event.fields!r}")
        if event.fields.get("phy_crc") != "passed":
            raise AssertionError("RX event did not carry verified PHY CRC")
        print(f"PASS: decoded {payload!r} with PHY CRC verified")
    finally:
        await backend.stop()


if __name__ == "__main__":
    asyncio.run(main())
