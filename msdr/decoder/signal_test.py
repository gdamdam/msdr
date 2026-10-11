"""Bounded, read-only protocol probe on an existing SDRconnect IQ span."""

import asyncio
import tempfile
from pathlib import Path

from msdr.decoder.band import BandDecoder
from msdr.decoder.registry import DECODERS, effective_binaries, resolve_binaries

_DEFAULT_VHF = ("dmr", "nxdn96", "pocsag")
_DEFAULT_UHF = ("dmr", "nxdn96", "p25")
_MAX_DECODERS = 3
_OBSERVE_S = 6.0
_OPTIMIZE_OFFSETS_HZ = (0, -2000, 2000, -4000, 4000)
_OPTIMIZE_OBSERVE_S = 2.5
_POLL_S = 0.25
_IQ_STARTUP_S = 8.0


def probe_decoders(
    frequency_hz: int, requested: list[str], binaries: dict[str, str | None]
) -> tuple[list[str], list[str]]:
    """Choose a small set of NFM/IQ-capable decoders and report unavailable ones."""
    choices = requested or list(_DEFAULT_VHF if frequency_hz < 300_000_000 else _DEFAULT_UHF)
    selected: list[str] = []
    unavailable: list[str] = []
    for decoder_id in dict.fromkeys(choices):
        spec = DECODERS.get(decoder_id)
        if spec is None or "NFM" not in spec.demodulators or spec.backend.startswith("standalone"):
            continue
        if all(binaries.get(name) for name in effective_binaries(spec, binaries)):
            selected.append(decoder_id)
        else:
            unavailable.append(decoder_id)
        if len(selected) == _MAX_DECODERS:
            break
    return selected, unavailable


async def _observe(band, decoders: list[str], observe_s: float) -> dict:
    evidence = {
        decoder_id: {
            "synchronized": False,
            "events": 0,
            "valid_frames": 0,
            "error": None,
            "running": False,
        }
        for decoder_id in decoders
    }
    first_status = band.status()
    first_frames = int(first_status.get("iq", {}).get("frames") or 0)
    frames = 0

    def collect(status):
        for channel in status.get("channels", []):
            for facts in channel.get("decoder_status", []):
                item = evidence.get(facts.get("id"))
                if item is None:
                    continue
                item["running"] |= facts.get("state") == "running"
                item["synchronized"] |= facts.get("synchronized") is True
                item["events"] = max(item["events"], int(facts.get("events") or 0))
                item["valid_frames"] = max(
                    item["valid_frames"], int(facts.get("valid_frames") or 0)
                )
                item["error"] = facts.get("error") or item["error"]

    collect(first_status)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + observe_s
    while loop.time() < deadline:
        await asyncio.sleep(min(_POLL_S, max(0, deadline - loop.time())))
        status = band.status()
        frames = max(frames, int(status.get("iq", {}).get("frames") or 0) - first_frames)
        collect(status)
    results = []
    for decoder_id, item in evidence.items():
        state = (
            "validated"
            if item["valid_frames"]
            else "synchronized"
            if item["synchronized"]
            else "output"
            if item["events"]
            else "error"
            if item["error"] or not item["running"]
            else "inconclusive"
        )
        results.append(
            {
                "id": decoder_id,
                "label": DECODERS[decoder_id].label,
                "state": state,
                "events": item["events"],
                "valid_frames": item["valid_frames"],
                "error": item["error"],
            }
        )
    found = any(r["state"] in {"validated", "synchronized", "output"} for r in results)
    all_errored = bool(results) and all(r["state"] == "error" for r in results)
    outcome = (
        "matched"
        if found
        else "no_iq"
        if not frames
        else "error"
        if all_errored
        else "inconclusive"
    )
    return {"outcome": outcome, "results": results, "frames": frames}


async def test_signal(
    *,
    frequency_hz: int,
    center_hz: int,
    sample_rate_hz: int,
    requested: list[str],
    bin_dirs: list[str],
    iq_stream_factory,
    observe_s: float = _OBSERVE_S,
    band_factory=BandDecoder,
    optimize: bool = False,
) -> dict:
    """Use one temporary IQ channel; never retune SDRconnect or publish events."""
    offsets = _OPTIMIZE_OFFSETS_HZ if optimize else (0,)
    if any(
        abs(frequency_hz + offset - center_hz) > sample_rate_hz / 2 - 25_000 for offset in offsets
    ):
        raise ValueError("signal is outside the current IQ span")
    binaries = resolve_binaries(bin_dirs)
    decoders, unavailable = probe_decoders(frequency_hz, requested, binaries)
    if not decoders:
        return {
            "frequency_hz": frequency_hz,
            "outcome": "unavailable",
            "results": [],
            "unavailable": unavailable,
            "frames": 0,
            "observation_s": 0,
        }

    with tempfile.TemporaryDirectory(prefix="msdr-signal-test-") as temp_dir:
        band = band_factory(
            iq_stream_factory=iq_stream_factory,
            decoders=decoders,
            binaries=binaries,
            center_hz=center_hz,
            sample_rate_hz=sample_rate_hz,
            memories=[(frequency_hz, "signal-test", decoders)],
            max_channels=0,
            threshold_db=10.0,
            idle_s=observe_s,
            spacing_hz=12500,
            on_event=lambda _event: None,
            on_status=lambda: None,
            detect=False,
            source_tag="signal-test",
            calls_dir=Path(temp_dir),
            fax_dir=Path(temp_dir),
        )
        trials = []
        recommendation = None
        recommended_decoder = None
        try:
            await asyncio.wait_for(band.start(), timeout=5)
            loop = asyncio.get_running_loop()
            ready_deadline = loop.time() + _IQ_STARTUP_S
            while not band.status().get("iq", {}).get("frames") and loop.time() < ready_deadline:
                await asyncio.sleep(0.1)
            if not band.status().get("iq", {}).get("frames"):
                return {
                    "frequency_hz": frequency_hz,
                    "outcome": "no_iq",
                    "results": [],
                    "trials": [],
                    "unavailable": unavailable,
                    "frames": 0,
                    "recommended_hz": None,
                    "recommended_decoder": None,
                }
            for offset in offsets:
                if offset:
                    await band.retune(
                        center_hz,
                        sample_rate_hz,
                        [(frequency_hz + offset, "signal-test", decoders)],
                    )
                duration = _OPTIMIZE_OBSERVE_S if optimize else observe_s
                trial = await _observe(band, decoders, duration)
                trials.append({"offset_hz": offset, **trial})
            if optimize:
                rank = {
                    "validated": 3,
                    "synchronized": 2,
                    "output": 1,
                    "inconclusive": 0,
                    "error": 0,
                }

                def score(trial):
                    return max((rank[r["state"]] for r in trial["results"]), default=0)

                best_score = max(map(score, trials))
                winners = [trial for trial in trials if score(trial) == best_score]
                best = winners[0]
                best_decoders = [r["id"] for r in best["results"] if rank[r["state"]] == best_score]
                better = (
                    len(winners) == 1
                    and best["offset_hz"] != 0
                    and len(best_decoders) == 1
                    and best_score >= 2
                    and best_score > score(trials[0])
                )
                if better:
                    # A burst in only one window cannot justify changing tuning.
                    # Repeat the winner and the original frequency.
                    await band.retune(
                        center_hz,
                        sample_rate_hz,
                        [(frequency_hz + best["offset_hz"], "signal-test", decoders)],
                    )
                    repeat_best = await _observe(band, decoders, _OPTIMIZE_OBSERVE_S)
                    await band.retune(
                        center_hz,
                        sample_rate_hz,
                        [(frequency_hz, "signal-test", decoders)],
                    )
                    repeat_base = await _observe(band, decoders, _OPTIMIZE_OBSERVE_S)
                    better = score(repeat_best) >= 2 and score(repeat_best) > score(repeat_base)
                recommendation = frequency_hz + best["offset_hz"] if better else None
                recommended_decoder = best_decoders[0] if better else None
        finally:
            # start() can fail after opening its socket or some child processes.
            await band.stop()
    baseline = trials[0]
    if optimize:
        outcome = (
            "improved"
            if recommendation is not None
            else "no_iq"
            if not any(t["frames"] for t in trials)
            else "unchanged"
        )
        return {
            "frequency_hz": frequency_hz,
            "outcome": outcome,
            "recommended_hz": recommendation,
            "recommended_decoder": recommended_decoder,
            "baseline": baseline,
            "trials": trials,
            "unavailable": unavailable,
        }
    return {
        "frequency_hz": frequency_hz,
        **baseline,
        "unavailable": unavailable,
        "observation_s": observe_s,
    }
