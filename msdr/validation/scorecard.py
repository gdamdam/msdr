"""corpus scorecard: runs the real analyzer/decoder code paths
against a *synthetic* corpus (msdr.validation.fixtures -- everything is
generated in code, nothing here reads a captured IQ file) and records, per
case, what actually happened: correct / incorrect / abstained / unavailable
per detector, and valid/invalid/unknown-integrity frame counts per decoder.

Nothing here is a claim of field accuracy -- see the header
`render_markdown` writes and docs/validation/scorecard-synthetic.md.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from msdr.decoder.pcm import cs16_to_iq as _sc16_to_iq
from msdr.decoder.pcm import iq_to_cs16 as _iq_to_sc16
from msdr.validation import fixtures as fx
from msdr.validation import impairments as imp

_DEFAULT_DURATION_S = 1.0
_DECODE_COLLECT_S = 2.5
_TAIL_SILENCE_S = 0.3


@dataclass
class DetectorOutcome:
    name: str  # "modulation" | "family" | "voice" | "keyed"
    outcome: str  # "correct" | "incorrect" | "abstained" | "unavailable" | "not_applicable"
    detail: str = ""


@dataclass
class DecoderOutcome:
    name: str  # "aprs" | "ais" | "rds" | "cw"
    status: str  # "available" | "unavailable" | "not_applicable"
    valid_frames: int = 0
    invalid_frames: int = 0
    unknown_integrity_frames: int = 0
    expected_payload_match: bool | None = None
    missed_events: int = 0
    false_output: int = 0
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    label: str
    is_negative: bool
    detectors: list[DetectorOutcome] = field(default_factory=list)
    decoders: list[DecoderOutcome] = field(default_factory=list)

    def decoder_events_total(self) -> int:
        return sum(
            d.valid_frames + d.invalid_frames + d.unknown_integrity_frames for d in self.decoders
        )

    def has_protocol_claim(self) -> bool:
        """Would this case's decoder output have supported a protocol claim
        (msdr.observations.recorder.decoder_spec: subject="protocol")? Only
        a decoder actually emitting a frame does -- an analyzer modulation/
        family guess is always evidence_type="iq_estimate", never identity
        (msdr.contracts.evidence.identity_established)."""
        return self.decoder_events_total() > 0


@dataclass
class ImpairmentResult:
    """One decoder x impairment-profile cell (msdr.validation.impairments).
    passed=None means the profile cannot be expressed for that decoder's
    input (e.g. NaN in int16 IQ) and was not run."""

    decoder: str
    profile: str
    passed: bool | None
    detail: str = ""


@dataclass
class ScorecardReport:
    generated_at: str
    cases: list[CaseResult]
    impairments: list[ImpairmentResult] = field(default_factory=list)


# --- shared: real classify_modulation/family_for/voice_score/keying_timing,
# called directly on synthetic features rather than through the full
# streaming Analyzer (msdr.survey.analyzer.Analyzer is round-robin/dwell
# stateful -- these are the same pure per-block functions it calls). -------


def _iq_features(iq: np.ndarray, rate: int) -> dict:
    """Same feature set the production analyzer feeds classify_modulation
    (survey/analyzer.py) -- including the 2-level FSK evidence
    (ifreq_bimodality / crossing grid) and the clock-line symbol rate, so
    the scorecard exercises the inference path that actually ships rather
    than a reduced one."""
    from msdr.survey.features import (
        BIMODAL_GATE,
        crossing_rate_hz,
        envelope_cv,
        ifreq_bimodality,
        ifreq_std_hz,
        occupied_bw_hz,
        snap_symbol_rate,
        spectral_symmetry,
        symbol_rate_hz,
    )

    features = {
        "env_cv": envelope_cv(iq),
        "ifreq_std_hz": ifreq_std_hz(iq, rate),
        "occ_bw_hz": occupied_bw_hz(iq, rate),
        "symmetry": spectral_symmetry(iq, rate),
        "active_fraction": 1.0,
        "active_s": len(iq) / rate,
    }
    crossing_rate, crossing_fit = crossing_rate_hz(iq, rate)
    features["ifreq_bimodality"] = ifreq_bimodality(iq, rate)
    features["crossing_fit"] = crossing_fit
    raw_rate, line_strength = symbol_rate_hz(iq, rate)
    features["fsk_line_strength"] = line_strength if raw_rate is not None else 0.0
    # Mirror the analyzer's crossing-grid override for 2-level FSK.
    crossing_snapped = snap_symbol_rate(crossing_rate)
    if (
        features["ifreq_bimodality"] >= BIMODAL_GATE
        and crossing_snapped is not None
        and snap_symbol_rate(raw_rate) != crossing_snapped
    ):
        raw_rate = crossing_rate
    features["symbol_rate_hz"] = snap_symbol_rate(raw_rate)
    return features


def _classify_iq(iq: np.ndarray, rate: int) -> tuple[dict, dict | None]:
    from msdr.survey.families import family_for
    from msdr.survey.features import classify_modulation

    features = _iq_features(iq, rate)
    modulation = classify_modulation(features)
    family = family_for(
        modulation["kind"], features["symbol_rate_hz"], "nfm", features["occ_bw_hz"]
    )
    return modulation, family


def _discriminator_audio(iq: np.ndarray, rate: int, deviation_hz: float = 3_000.0) -> np.ndarray:
    """Minimal FM discriminator (angle of the sample-to-sample product),
    scaled so `deviation_hz` maps to 1.0 -- same idea as the receiver's flat
    tap / analyzer.py's own `_discriminator_audio`, just local so this module
    doesn't reach into that private helper."""
    disc = np.angle(iq[1:] * np.conj(iq[:-1])) * rate / (2 * np.pi * deviation_hz)
    return disc.astype(np.float32)


# --- decoder runners ---------------------------------------------------------


async def _run_line_decoder(
    decoder_id: str,
    audio: np.ndarray,
    binaries: dict[str, str | None],
    *,
    collect_s: float = _DECODE_COLLECT_S,
    negative: bool = False,
    expected_text: str | None = None,
) -> DecoderOutcome:
    from msdr.decoder.backends import DecoderError, build_backend
    from msdr.decoder.registry import DECODERS, effective_binaries

    spec = DECODERS.get(decoder_id)
    if spec is None:
        return DecoderOutcome(decoder_id, "unavailable", detail="not in registry yet")
    missing = [n for n in effective_binaries(spec, binaries) if not binaries.get(n)]
    if missing:
        detail = f"missing binary: {', '.join(missing)}"
        return DecoderOutcome(decoder_id, "unavailable", detail=detail)
    try:
        backend = build_backend(spec, binaries)
    except DecoderError as exc:
        return DecoderOutcome(decoder_id, "unavailable", detail=str(exc))

    tail = np.zeros(int(_TAIL_SILENCE_S * spec.input_rate), dtype=np.float32)
    pcm = fx.s16_pcm(np.concatenate([audio, tail]))

    events = []
    await backend.start()
    try:
        backend.feed(pcm)
        gen = backend.events()
        deadline = time.monotonic() + collect_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                events.append(await asyncio.wait_for(gen.__anext__(), timeout=remaining))
            except TimeoutError:
                break
    finally:
        await backend.stop()

    valid = sum(1 for e in events if e.integrity == "passed")
    invalid = sum(1 for e in events if e.integrity == "failed")
    unknown = len(events) - valid - invalid
    # "False output" is output on a *negative* control; on a positive case
    # every event is the expected kind of output, and the payload column says
    # whether the decoded text carries what the fixture encoded.
    payload_match: bool | None = None
    if expected_text is not None:
        payload_match = any(expected_text in (e.text or "") for e in events)
    return DecoderOutcome(
        decoder_id,
        "available",
        valid_frames=valid,
        invalid_frames=invalid,
        unknown_integrity_frames=unknown,
        expected_payload_match=payload_match,
        false_output=len(events) if negative else 0,
        detail=f"{len(events)} event(s) via {spec.backend}",
    )


def _run_ais(seed: int = 0) -> DecoderOutcome:
    try:
        from msdr.decoder.pydecoders.ais import AisDecoder
    except ImportError as exc:
        return DecoderOutcome("ais", "unavailable", detail=f"AisDecoder not importable yet: {exc}")
    try:
        from msdr.validation.fixtures_ais import ais_type1, iq_to_flat, make_burst
    except ImportError as exc:
        return DecoderOutcome(
            "ais", "unavailable", detail=f"tests/helpers/gmsk.py not importable: {exc}"
        )

    rng = np.random.default_rng(seed)
    mmsi = 366123456
    payload = ais_type1(mmsi=mmsi, nav_status=0, sog_tenths=123, lon_deg=-122.4194, lat_deg=37.8081)
    iq = make_burst(payload, 48_000.0, rng)
    flat = iq_to_flat(iq, fs=48_000.0)
    decoder = AisDecoder()
    events = list(decoder.feed(flat)) + list(decoder.flush())
    valid = [e for e in events if e.integrity == "passed"]
    payload_match = any(e.fields.get("mmsi") == mmsi for e in valid)
    return DecoderOutcome(
        "ais",
        "available",
        valid_frames=len(valid),
        unknown_integrity_frames=len(events) - len(valid),
        expected_payload_match=payload_match,
        detail=f"{len(events)} event(s), stats={decoder.stats()}",
    )


def _run_ais_negative(audio: np.ndarray) -> DecoderOutcome:
    """audio: float32 mono at fixtures.AIS_RATE_HZ (the NFM flat-tap rate
    AisDecoder expects) -- a negative-control input fed straight in, no GMSK
    burst anywhere in it."""
    try:
        from msdr.decoder.pydecoders.ais import AisDecoder
    except ImportError as exc:
        return DecoderOutcome("ais", "unavailable", detail=f"AisDecoder not importable yet: {exc}")
    decoder = AisDecoder()
    events = list(decoder.feed(audio.astype(np.float32))) + list(decoder.flush())
    valid = [e for e in events if e.integrity == "passed"]
    return DecoderOutcome(
        "ais",
        "available",
        valid_frames=len(valid),
        false_output=len(events),
        detail=str(decoder.stats()),
    )


def _run_rds(pilot_level: float) -> DecoderOutcome:
    try:
        from msdr.survey.rds import RdsReceiver, encode_rds_mpx, fm_modulate_chunks
    except ImportError as exc:
        return DecoderOutcome("rds", "unavailable", detail=str(exc))

    fs, mpx_fs, offset_hz = 8_000_000, 250_000, 1_300_000
    ps, pi, rt = "TESTFM", 0x54A8, "Hello RDS world"
    mpx = encode_rds_mpx(ps, pi, rt, 3.0, mpx_fs, pilot_level=pilot_level)
    rx = RdsReceiver(fs, offset_hz)
    for chunk in fm_modulate_chunks(mpx, mpx_fs, fs, offset_hz, 400_000, amplitude=0.3):
        rx.process(chunk)
    result = rx.parser.result()
    pi_ok = result.get("pi") == f"0x{pi:04X}"
    ps_ok = result.get("ps") == ps
    return DecoderOutcome(
        "rds",
        "available",
        valid_frames=1 if pi_ok else 0,
        expected_payload_match=pi_ok and ps_ok,
        detail=f"pi={result.get('pi')} ps={result.get('ps')!r} groups={result.get('groups')}",
    )


# --- cases -------------------------------------------------------------------


def _detector(name: str, ok: bool, detail: str, *, unavailable: bool = False) -> DetectorOutcome:
    if unavailable:
        return DetectorOutcome(name, "unavailable", detail)
    return DetectorOutcome(name, "correct" if ok else "incorrect", detail)


async def _case_nfm_voice(binaries: dict) -> CaseResult:
    iq = fx.nfm_voice_like_iq(_DEFAULT_DURATION_S)
    modulation, family = _classify_iq(iq, fx.IQ_RATE_HZ)
    audio = _discriminator_audio(iq, fx.IQ_RATE_HZ) * 5_000.0  # NFM flat-tap scale (5 kHz -> 1.0)
    from msdr.survey.features import voice_score

    voice = voice_score(audio.astype(np.float64), fx.IQ_RATE_HZ)
    return CaseResult(
        "nfm_voice_like",
        "NFM voice-like (speech-shaped noise)",
        is_negative=False,
        detectors=[
            _detector(
                "modulation", modulation["kind"] in ("fm_fsk", "carrier"), modulation["kind"]
            ),
            _detector("voice", voice["kind"] in ("voice", "data"), voice["kind"]),
        ],
    )


async def _case_afsk1200(binaries: dict) -> CaseResult:
    text = ">msdr synthetic test fixture"
    audio = fx.aprs_ui_frame_audio(text)
    decoder = await _run_line_decoder("aprs", audio, binaries, expected_text=text)
    return CaseResult(
        "afsk1200_ax25",
        "AFSK-1200 AX.25 (packet/APRS)",
        is_negative=False,
        decoders=[decoder],
    )


async def _case_keyed_carrier(binaries: dict) -> CaseResult:
    from msdr.survey.families import family_for
    from msdr.survey.features import keying_timing

    runs = fx.keyed_carrier_on_runs_s(baud=150.0, duration_s=1.5)
    keying = keying_timing(runs)
    family = family_for("ook", 150.0, "nfm", 1_000.0, keying=keying)
    name = (family or {}).get("name", "")
    return CaseResult(
        "keyed_carrier_ook_20baud",
        "keyed carrier (OOK, non-Morse timing)",
        is_negative=False,
        detectors=[
            _detector(
                "family",
                name != "CW",
                f"family={name!r} fits_cw={keying.get('fits_cw')}",
            )
        ],
    )


async def _case_am_tone(binaries: dict) -> CaseResult:
    iq = fx.am_tone_iq(_DEFAULT_DURATION_S)
    modulation, _family = _classify_iq(iq, fx.IQ_RATE_HZ)
    return CaseResult(
        "am_tone",
        "AM tone",
        is_negative=False,
        detectors=[
            _detector("modulation", modulation["kind"] in ("am", "carrier"), modulation["kind"])
        ],
    )


async def _case_ssb_tone(binaries: dict, *, usb: bool) -> CaseResult:
    iq = fx.ssb_tone_iq(_DEFAULT_DURATION_S, usb=usb)
    modulation, _family = _classify_iq(iq, fx.IQ_RATE_HZ)
    label = "USB tone" if usb else "LSB tone"
    kind = modulation["kind"]
    # No tested SSB-vs-AM/FM discriminator exists yet -- "unknown" here is an
    # honest abstention (features.classify_modulation says so itself), not a
    # wrong call, so it's graded separately rather than as "incorrect".
    if kind == "unknown":
        modulation_outcome = "abstained"
    elif kind in ("am", "carrier"):
        modulation_outcome = "correct"
    else:
        modulation_outcome = "incorrect"
    outcome = DetectorOutcome("modulation", modulation_outcome, kind)
    return CaseResult(
        "usb_tone" if usb else "lsb_tone",
        label,
        is_negative=False,
        detectors=[outcome],
    )


async def _case_cw(binaries: dict) -> CaseResult:
    from msdr.survey.families import family_for
    from msdr.survey.features import keying_timing

    runs = fx.morse_on_runs_s("CQ CQ")
    keying = keying_timing(runs)
    family = family_for("ook", 20.0, "nfm", 500.0, keying=keying)
    name = (family or {}).get("name", "")
    audio = fx.morse_cw_audio("CQ CQ")
    decoder = await _run_line_decoder("cw", audio, binaries)
    return CaseResult(
        "cw_proper_timing",
        "CW (proper Morse timing)",
        is_negative=False,
        detectors=[
            _detector("family", name == "CW", f"family={name!r} fits_cw={keying.get('fits_cw')}")
        ],
        decoders=[decoder],
    )


async def _case_rds(*, pilot_level: float, case_id: str, label: str) -> CaseResult:
    decoder = _run_rds(pilot_level)
    return CaseResult(case_id, label, is_negative=False, decoders=[decoder])


async def _case_ais(binaries: dict) -> CaseResult:
    decoder = _run_ais()
    return CaseResult(
        "gmsk9600_ais_burst",
        "GMSK 9600 AIS burst",
        is_negative=False,
        decoders=[decoder],
    )


async def _negative_decoders(kind: str, binaries: dict) -> list[DecoderOutcome]:
    """Same negative-control signal, generated fresh at each decoder's own
    input rate (fixtures.AUDIO_RATE_HZ for aprs/cw, fixtures.AIS_RATE_HZ for
    ais) rather than resampled -- both are cheap to regenerate."""
    if kind == "white_noise":
        audio_22k = fx.white_noise_audio(_DEFAULT_DURATION_S, rate=fx.AUDIO_RATE_HZ, seed=1)
        audio_48k = fx.white_noise_audio(_DEFAULT_DURATION_S, rate=fx.AIS_RATE_HZ, seed=1)
    elif kind == "steady_tone":
        audio_22k = fx.steady_tone_audio(duration_s=_DEFAULT_DURATION_S, rate=fx.AUDIO_RATE_HZ)
        audio_48k = fx.steady_tone_audio(duration_s=_DEFAULT_DURATION_S, rate=fx.AIS_RATE_HZ)
    elif kind == "clipped":
        audio_22k = fx.clipped_audio(
            fx.steady_tone_audio(duration_s=_DEFAULT_DURATION_S, rate=fx.AUDIO_RATE_HZ)
        )
        audio_48k = fx.clipped_audio(
            fx.steady_tone_audio(duration_s=_DEFAULT_DURATION_S, rate=fx.AIS_RATE_HZ)
        )
    elif kind == "dropped_blocks":
        audio_22k = fx.dropped_blocks_audio(
            fx.steady_tone_audio(duration_s=_DEFAULT_DURATION_S, rate=fx.AUDIO_RATE_HZ)
        )
        audio_48k = fx.dropped_blocks_audio(
            fx.steady_tone_audio(duration_s=_DEFAULT_DURATION_S, rate=fx.AIS_RATE_HZ)
        )
    else:
        raise ValueError(kind)
    aprs = await _run_line_decoder("aprs", audio_22k, binaries, negative=True)
    cw = await _run_line_decoder("cw", audio_22k, binaries, negative=True)
    ais = _run_ais_negative(audio_48k)
    return [aprs, cw, ais]


async def _case_white_noise(binaries: dict) -> CaseResult:
    from msdr.survey.features import voice_score

    audio = fx.white_noise_audio(_DEFAULT_DURATION_S, seed=1)
    iq = fx.white_noise_iq(_DEFAULT_DURATION_S)
    voice = voice_score(audio.astype(np.float64), fx.AUDIO_RATE_HZ)
    modulation, family = _classify_iq(iq, fx.IQ_RATE_HZ)
    decoders = await _negative_decoders("white_noise", binaries)
    return CaseResult(
        "white_noise",
        "white noise (negative control)",
        is_negative=True,
        detectors=[
            _detector("voice", voice["kind"] != "voice", voice["kind"]),
            _detector(
                "family",
                not (family or {}).get("suggested_decoders"),
                f"family={(family or {}).get('name')!r}",
            ),
        ],
        decoders=decoders,
    )


async def _case_steady_tone(binaries: dict) -> CaseResult:
    decoders = await _negative_decoders("steady_tone", binaries)
    return CaseResult(
        "single_steady_tone",
        "single steady tone (negative control)",
        is_negative=True,
        decoders=decoders,
    )


async def _case_clipped(binaries: dict) -> CaseResult:
    decoders = await _negative_decoders("clipped", binaries)
    return CaseResult(
        "clipped_iq",
        "clipped IQ / hard-limited (negative control)",
        is_negative=True,
        decoders=decoders,
    )


async def _case_adjacent_strong_channel(binaries: dict) -> CaseResult:
    iq = fx.adjacent_strong_channel_iq(_DEFAULT_DURATION_S)
    modulation, family = _classify_iq(iq, fx.IQ_RATE_HZ)
    return CaseResult(
        "adjacent_strong_channel",
        "adjacent strong channel (negative control, detector-only)",
        is_negative=True,
        detectors=[
            _detector(
                "family",
                not (family or {}).get("suggested_decoders"),
                f"family={(family or {}).get('name')!r}",
            )
        ],
        decoders=[DecoderOutcome(name, "not_applicable") for name in ("aprs", "cw", "ais")],
    )


async def _case_dropped_blocks(binaries: dict) -> CaseResult:
    decoders = await _negative_decoders("dropped_blocks", binaries)
    return CaseResult(
        "dropped_blocks",
        "dropped blocks (negative control)",
        is_negative=True,
        decoders=decoders,
    )


# --- impairment profiles: decoders with a synthetic IQ generator ----------
# A decoder fed only by the app's own clean generator proves the receiver
# agrees with the generator. Each runner builds the same clean burst the
# decoder's own tests use, degrades it with msdr.validation.impairments and
# records whether the decoder still recovers the known payload. A failing
# profile is a real (synthetic) result, not something to tune away.

_ADSB_HEX = "8D485020994409940838175B284F"  # published velocity frame (tests/decoder)
_ADSB_RATE = 4_000_000  # 2 samples/chip, integer path of NativeADSBBackend


def _adsb_burst_iq(rate: int = _ADSB_RATE, amplitude: float = 0.5) -> np.ndarray:
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(_ADSB_HEX), dtype=np.uint8)).astype(np.float32)
    preamble = np.zeros(16, dtype=np.float32)
    preamble[[0, 2, 7, 9]] = 1.0
    chips = np.empty(len(bits) * 2, dtype=np.float32)
    chips[0::2], chips[1::2] = bits, 1.0 - bits
    chips = np.concatenate([preamble, chips])
    spc = rate // 2_000_000
    burst = np.repeat(chips, spc) * amplitude
    pad = np.zeros(rate // 500, dtype=np.float32)  # 2 ms of silence each side
    return np.concatenate([pad, burst, pad]).astype(np.complex64)


def _drain(backend) -> list:
    out = []
    while not backend._events.empty():
        event = backend._events.get_nowait()
        if event is not None:
            out.append(event)
    return out


def _impair_ais(profile: str) -> tuple[bool, str]:
    from msdr.decoder.pydecoders.ais import AisDecoder
    from msdr.validation.fixtures_ais import ais_type1, iq_to_flat, make_burst

    mmsi = 366123456
    payload = ais_type1(mmsi=mmsi, nav_status=0, sog_tenths=123, lon_deg=-122.4194, lat_deg=37.8081)
    iq = make_burst(payload, 48_000.0, np.random.default_rng(0))
    # Silence either side so the decoder sees a burst, not a continuous signal.
    iq = np.concatenate([np.zeros(2400, np.complex64), iq, np.zeros(2400, np.complex64)])
    iq = imp.apply_profile(profile, iq.astype(np.complex64), 48_000.0, 0, 162e6)
    flat = iq_to_flat(iq, fs=48_000.0)
    decoder = AisDecoder()
    events = list(decoder.feed(flat)) + list(decoder.flush())
    ok = any(e.integrity == "passed" and e.fields.get("mmsi") == mmsi for e in events)
    return ok, f"{len(events)} event(s), stats={decoder.stats()}"


async def _impair_adsb(profile: str) -> tuple[bool, str]:
    from msdr.decoder.pydecoders.adsb import NativeADSBBackend

    iq = imp.apply_profile(profile, _adsb_burst_iq(), float(_ADSB_RATE), 0, 1090e6)
    backend = NativeADSBBackend(None, sample_rate_hz=_ADSB_RATE, center_hz=0.0, frequency_hz=0.0)
    await backend.start()
    backend.feed(_iq_to_sc16(iq))
    events = _drain(backend)
    icao = _ADSB_HEX[2:8]
    ok = any(e.integrity == "passed" and e.fields.get("icao") == icao for e in events)
    return ok, f"{len(events)} event(s), error={backend.error!r}"


async def _impair_rs41(profile: str) -> tuple[bool, str]:
    from msdr.decoder.pydecoders.rs41 import RS41Backend
    from msdr.validation.fixtures_rs41 import rs41_iq, rs41_test_frame

    iq = _sc16_to_iq(rs41_iq(rs41_test_frame(), leading_samples=5))
    iq = imp.apply_profile(profile, iq, 57_600.0, 0, 403e6)
    backend = RS41Backend(None, sample_rate_hz=57_600, center_hz=403e6, frequency_hz=403e6)
    await backend.start()
    backend.feed(_iq_to_sc16(iq))
    events = _drain(backend)
    ok = any(e.integrity == "passed" and e.fields.get("serial") == "K1234567" for e in events)
    return ok, f"{len(events)} event(s)"


_IMPAIRMENT_DECODERS = {"ais": _impair_ais, "adsb": _impair_adsb, "rs41": _impair_rs41}
# AIS is fed float flat audio, so NaN can occur; the sc16 IQ decoders only see int16.
_SC16_DECODERS = {"adsb", "rs41"}


async def run_impairments() -> list[ImpairmentResult]:
    results = []
    for decoder, runner in _IMPAIRMENT_DECODERS.items():
        for name, profile in imp.PROFILES.items():
            if profile.finite_only and decoder in _SC16_DECODERS:
                results.append(
                    ImpairmentResult(decoder, name, None, "int16 IQ cannot carry NaN/inf")
                )
                continue
            try:
                out = runner(name)
                ok, detail = await out if asyncio.iscoroutine(out) else out
                results.append(ImpairmentResult(decoder, name, ok, detail))
            except ImportError as exc:
                results.append(ImpairmentResult(decoder, name, None, f"unavailable: {exc}"))
            except Exception as exc:  # noqa: BLE001 -- a crash is a failing cell, not a scorecard abort
                results.append(
                    ImpairmentResult(decoder, name, False, f"raised {type(exc).__name__}: {exc}")
                )
    return results


async def run_scorecard() -> ScorecardReport:
    from msdr.decoder.registry import resolve_binaries

    binaries = resolve_binaries([])
    cases = [
        await _case_nfm_voice(binaries),
        await _case_afsk1200(binaries),
        await _case_keyed_carrier(binaries),
        await _case_am_tone(binaries),
        await _case_ssb_tone(binaries, usb=True),
        await _case_ssb_tone(binaries, usb=False),
        await _case_cw(binaries),
        await _case_rds(pilot_level=0.09, case_id="wfm_rds_pilot", label="WFM + RDS (pilot)"),
        await _case_rds(
            pilot_level=0.0, case_id="wfm_rds_pilotless", label="WFM + RDS (pilotless)"
        ),
        await _case_ais(binaries),
        await _case_white_noise(binaries),
        await _case_steady_tone(binaries),
        await _case_clipped(binaries),
        await _case_adjacent_strong_channel(binaries),
        await _case_dropped_blocks(binaries),
    ]
    return ScorecardReport(
        generated_at=datetime.now(UTC).isoformat(),
        cases=cases,
        impairments=await run_impairments(),
    )


# --- Markdown rendering -------------------------------------------------------

_HEADER = """# Corpus scorecard (synthetic fixtures)

**Synthetic fixtures only -- not a claim of field accuracy.** Every case
below is generated in code (`msdr.validation.fixtures`); none of it is a
captured off-air recording, and a pass here says only that msdr's real
analyzer/decoder code paths behave correctly on a *known* synthetic signal.
Field validation (a real off-air capture) is tracked separately --
see `docs/validation/corpus.md` and `tests/corpus/manifest.json`.

Generated: {generated_at}
"""


def render_markdown(report: ScorecardReport) -> str:
    lines = [_HEADER.format(generated_at=report.generated_at)]
    lines.append("## Detectors\n")
    lines.append("| case | detector | outcome | detail |")
    lines.append("|---|---|---|---|")
    for case in report.cases:
        for d in case.detectors:
            lines.append(f"| {case.label} | {d.name} | {d.outcome} | {d.detail} |")
    lines.append("\n## Decoders\n")
    lines.append(
        "| case | decoder | status | valid | invalid | unknown | "
        "payload match | false output | detail |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for case in report.cases:
        for dec in case.decoders:
            lines.append(
                f"| {case.label} | {dec.name} | {dec.status} | {dec.valid_frames} | "
                f"{dec.invalid_frames} | {dec.unknown_integrity_frames} | "
                f"{dec.expected_payload_match} | {dec.false_output} | {dec.detail} |"
            )
    lines.append("\n## Negative controls\n")
    lines.append("| case | decoder events | protocol/station claim |")
    lines.append("|---|---|---|")
    for case in report.cases:
        if not case.is_negative:
            continue
        lines.append(
            f"| {case.label} | {case.decoder_events_total()} | {case.has_protocol_claim()} |"
        )
    if report.impairments:
        lines.extend(_render_impairments(report.impairments))
    return "\n".join(lines) + "\n"


def _render_impairments(results: list[ImpairmentResult]) -> list[str]:
    profiles = list(dict.fromkeys(r.profile for r in results))
    decoders = list(dict.fromkeys(r.decoder for r in results))
    by_key = {(r.decoder, r.profile): r for r in results}
    lines = [
        "\n## Impairment profiles (synthetic)\n",
        "Each decoder's known synthetic burst is degraded by "
        "`msdr.validation.impairments` and decoded; pass = the expected payload "
        "is recovered with a passed integrity check. `n/a` = profile not "
        "expressible for that decoder's input. Failures are reported as found, "
        "not tuned away. Synthetic only -- not off-air validation.\n",
        "| decoder | " + " | ".join(profiles) + " |",
        "|---|" + "---|" * len(profiles),
    ]
    for decoder in decoders:
        cells = []
        for profile in profiles:
            r = by_key.get((decoder, profile))
            cells.append("n/a" if r is None or r.passed is None else "pass" if r.passed else "FAIL")
        lines.append(f"| {decoder} | " + " | ".join(cells) + " |")
    failures = [r for r in results if r.passed is False]
    if failures:
        lines.append("\nFailing cells:\n")
        lines.extend(f"- {r.decoder} / {r.profile}: {r.detail}" for r in failures)
    return lines
