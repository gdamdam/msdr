"""Capability matrix: per-decoder facts about input path, what integrity
signal (if any) the backend exposes, and what the repo's test suite has
actually shown to work -- kept data-only, no process/IO logic here (same
rule as registry.py).

Distinct from DecoderSpec (registry.py): DecoderSpec is what msdr *runs*
(backend, argv, rates). CapabilityEntry is what's *known* about that
decoder's reliability -- a separate, additive fact table keyed by the same
decoder_id. RDS is decoded by msdr/survey/rds.py, not run through DECODERS
(no manager channel, no backend process), so it gets its own RDS_ENTRY
rather than a MATRIX slot.

Sources for every field below: msdr/decoder/registry.py (DecoderSpec),
msdr/decoder/manager.py (all_nfm / iq_only routing, _start_decoding_iq),
msdr/decoder/band.py + manager.start_band (the "NFM" in demodulators gate),
msdr/decoder/backends.py and msdr/decoder/pydecoders/* (what's actually
parsed/checked), and the existing tests under tests/decoder/ (grepped per
id -- see each `validation.tests` list). Nothing here claims a check the
cited code doesn't perform.
"""

from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

import msdr
from msdr.contracts.readiness import Validation

InputPath = Literal["audio", "iq", "both"]
IntegrityPolicy = Literal[
    "crc", "fcs", "parity", "fec", "repeat_fields", "none_exposed", "builtin_check"
]

# All Validation records below were written against this ship date (README
# "Shipped in 0.15.0-0.18.0 (2026-09-25)") -- the
# msdr version they're pinned to is the current one (python decoders are
# versioned with msdr itself; see contracts/readiness.py's validation_stale).
_SHIPPED_ON = date(2026, 9, 25)
_DSDFME_EXTRA_TESTED_ON = date(2026, 9, 27)
_SSTV_TESTED_ON = date(2026, 10, 1)
_RTTY_TESTED_ON = date(2026, 10, 3)
_MSDR_VERSION = msdr.__version__


@dataclass(frozen=True)
class CapabilityEntry:
    input_path: InputPath
    demodulators: tuple[str, ...]
    # Startable as the one decoder for the current VFO/frequency (manager.
    # start_decoding, via the audio path or the single fixed IQ channel) --
    # true for every entry here; kept explicit because band_path (multi-
    # channel detection across the whole span) is not.
    single_channel: bool
    # manager.start_band / BandDecoder gate is exactly `"NFM" in
    # spec.demodulators` (msdr/decoder/manager.py start_band) -- true for
    # the 9 NFM decoders regardless of iq_only, false for HF/AM/CW ones.
    band_path: bool
    replay_supported: Literal["planned_s3", "raw_iq"]
    integrity_policy: IntegrityPolicy
    integrity_notes: str
    validation: Validation
    field_notes: str


# -- shared notes reused verbatim where several ids share the same evidence --

_MULTIMON_JSON_NO_FLAG = (
    "multimon-ng emits one JSON object per decoded message; msdr's "
    "parse_multimon_json() (backends.py) passes every field multimon prints "
    "through unchanged and adds none of its own -- if multimon applies "
    "internal error correction it does not surface a pass/fail flag, so "
    "msdr cannot report per-message validity."
)
_AX25_FCS_NOTE = (
    "AX.25/HDLC framing: both multimon-ng's AFSK1200/FSK9600 demodulators and "
    "direwolf verify the frame's 16-bit FCS before printing a line at all -- "
    "a bad-FCS frame is silently dropped, never emitted as a DecodeEvent, so "
    "every line msdr's parser sees already passed the check (no separate "
    "flag needed)."
)
_DSDFME_NO_FLAG = (
    "dsd-fme performs its own protocol-level FEC/CRC internally (DMR "
    "BPTC/CRC-CCITT, P25 Golay/CRC, NXDN FEC, and M17 CRC) but msdr's "
    "parse_dsdfme_line() (backends.py) reads synchronization and reported "
    "metadata only -- no validity/error flag is parsed or exposed."
)

MATRIX: dict[str, CapabilityEntry] = {
    "pocsag": CapabilityEntry(
        input_path="both",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=("POCSAG codewords carry BCH(31,21) FEC; " + _MULTIMON_JSON_NO_FLAG),
        validation=Validation(
            state="live_output_observed",
            backend_version="1.6.1",
            validated_on=date(2026, 10, 3),
            tests=[
                "tests/decoder/test_backends.py::test_parse_multimon_json_pocsag_sample",
                "tests/decoder/test_backends.py::test_parse_multimon_json_drops_empty_tone_only_page",
                "tests/decoder/test_backends.py::test_parse_multimon_json_keeps_empty_text_with_address",
                "tests/decoder/test_backends.py::test_pocsag_argv_hides_empty_messages",
                "tests/decoder/test_backends.py::test_build_backend_pocsag_uses_json_parser",
            ],
            notes=(
                "Live POCSAG1200 output observed at 152.840 MHz after the 2026-10-03 "
                "fixed-channel repair; operator confirms further pages decode. "
                "multimon-ng exposes no per-message integrity flag; independent "
                "content comparison remains pending."
            ),
            report="docs/validation/live-vhf-uhf-2026-10-03.md",
        ),
        field_notes=(
            "Live pages observed on 2026-09-25 and 2026-10-03; no independent "
            "verification of decoded message content."
        ),
    ),
    "flex": CapabilityEntry(
        input_path="both",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "FLEX has its own BCH-style checksum per block; " + _MULTIMON_JSON_NO_FLAG
        ),
        validation=Validation(
            state="untested",
            backend_version=None,
            validated_on=None,
            tests=[],
            notes=(
                "No flex-id fixture test exists; parse_multimon_json() is decoder_id-agnostic "
                "and is exercised generically under the pocsag/eas ids "
                "(tests/decoder/test_backends.py), which covers this code path but not FLEX's "
                "own field shapes/demod names specifically."
            ),
        ),
        field_notes="Not verified off-air; ships since 0.15.0 (2026-09-25), synthetic tests only.",
    ),
    "eas": CapabilityEntry(
        input_path="both",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "SAME/EAS headers are conventionally sent 3x for redundancy, but "
            + _MULTIMON_JSON_NO_FLAG
            + " No cross-repeat consistency check is visible in msdr's own code."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_multimon_json_prefers_numeric_then_message"
            ],
            notes="Synthetic JSON line fixture only; no off-air SAME header observed.",
        ),
        field_notes="Not verified off-air; ships since 0.15.0 (2026-09-25), synthetic tests only.",
    ),
    "aprs": CapabilityEntry(
        input_path="both",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="fcs",
        integrity_notes=_AX25_FCS_NOTE,
        validation=Validation(
            state="live_output_observed",
            backend_version="1.8.1",
            validated_on=date(2026, 10, 3),
            tests=[
                "tests/decoder/test_backends.py::test_parse_multimon_plain_aprs_style",
                "tests/decoder/test_backends.py::test_build_backend_aprs_uses_plain_parser",
                "tests/decoder/test_backends.py::test_build_backend_aprs_prefers_direwolf_when_present",
                "tests/decoder/test_backends.py::test_build_backend_aprs_uses_multimon_without_direwolf",
                "tests/decoder/test_backends.py::test_parse_direwolf_line_sample",
                "tests/decoder/test_backends.py::test_parse_direwolf_line_ignores_chatter",
                "tests/decoder/test_backends.py::test_parse_direwolf_line_ignores_transmit_echo",
            ],
            notes=(
                "Live APRS output confirmed by the operator on 2026-10-03; "
                "an earlier 144.390 MHz Direwolf frame passed AX.25 FCS. "
                "Independent payload comparison remains pending."
            ),
            report="docs/validation/live-vhf-uhf-2026-10-03.md",
        ),
        field_notes=(
            "Live packet with FCS pass recorded 2026-09-28; operator reconfirmed "
            "APRS decoding 2026-10-03. Exact payload still needs independent comparison."
        ),
    ),
    "packet9600": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="fcs",
        integrity_notes=_AX25_FCS_NOTE,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_multimon_plain_multi_hop_path",
                "tests/decoder/test_backends.py::test_build_backend_packet9600_prefers_direwolf_with_9600_baud",
            ],
            notes="Synthetic FSK9600 TNC2 line fixture only; no off-air 9600-baud packet captured.",
        ),
        field_notes="Not verified off-air; ships since 0.16.0 (2026-09-25), synthetic tests only.",
    ),
    "packet300": CapabilityEntry(
        input_path="audio",
        demodulators=("USB", "LSB"),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="fcs",
        integrity_notes=_AX25_FCS_NOTE,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_build_backend_packet300_requires_direwolf"
            ],
            notes=(
                "Backend-construction test only; direwolf line parsing covered "
                "generically, not HF-specific."
            ),
        ),
        field_notes=(
            "Not verified off-air (HF); ships since 0.16.0 (2026-09-25), synthetic tests only."
        ),
    ),
    "dtmf": CapabilityEntry(
        input_path="both",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "DTMF is a tone-pair alphabet with no error-detection concept; "
            + _MULTIMON_JSON_NO_FLAG
        ),
        validation=Validation(
            state="untested",
            backend_version=None,
            validated_on=None,
            tests=[],
            notes=(
                "No dtmf-id test exists; covered only indirectly by the generic "
                "parse_multimon_json tests."
            ),
        ),
        field_notes="Not verified off-air; ships since 0.15.0 (2026-09-25), no dedicated test.",
    ),
    "cw": CapabilityEntry(
        input_path="audio",
        demodulators=("CW",),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "Morse has no error-detection code; multimon-ng emits characters/words as heard."
        ),
        validation=Validation(
            state="live_output_observed",
            backend_version="1.6.1",
            validated_on=date(2026, 10, 3),
            tests=[
                "tests/decoder/test_backends.py::test_parse_multimon_plain_drops_empty_text_no_payload",
                "tests/decoder/test_backends.py::test_parse_multimon_plain_without_colon_is_none",
                "tests/decoder/test_backends.py::test_parse_multimon_plain_cw_has_no_tnc2_fields",
            ],
            notes=(
                "Live CW text observed at 432.304620 MHz after exact tuning; "
                "operator confirms decoder output. Morse has no integrity check "
                "and independent transcription comparison remains pending."
            ),
            report="docs/validation/live-vhf-uhf-2026-10-03.md",
        ),
        field_notes="Live CW text observed 2026-10-03; independent transcription pending.",
    ),
    "rtty": CapabilityEntry(
        input_path="audio",
        demodulators=("USB", "LSB"),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "Ham RTTY (Baudot/ITA2) carries no parity/FEC; msdr.decoder.pydecoders.rtty "
            "(native, ported from SDRangel's RttyDemodSink) checks stop-bit framing before "
            "emitting a character, which is not an error-detecting code."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_RTTY_TESTED_ON,
            tests=[
                "tests/decoder/test_rtty.py::test_auto_detects_rate_shift_and_polarity",
                "tests/decoder/test_rtty.py::test_noise_only_has_no_characters",
                "tests/decoder/test_rtty.py::test_exact_text_above_stated_snr",
                "tests/decoder/test_rtty.py::test_preset_mode_locks_without_estimating_baud",
                "tests/decoder/test_rtty.py::test_decodes_minimodem_reference_wav",
            ],
            notes=(
                "Synthesized 2-tone FSK only (plus a minimodem-generated reference WAV when "
                "minimodem is installed); no off-air RTTY captured. Auto mode measured exactly "
                "recovering baud (snapped to the common rate) and within 5 Hz (170 Hz case) to "
                "21 Hz (850 Hz case) of the true shift at 45.45/170, 50/450 and 75/850 Bd, "
                "including an inverted-shift case; clean on 15 s of noise-only input "
                "(0 characters)."
            ),
        ),
        field_notes=(
            "Not verified off-air (HF); native auto-detecting decoder since 0.62.3 "
            "(2026-10-03), synthetic tests only."
        ),
    ),
    "rtty_minimodem": CapabilityEntry(
        input_path="audio",
        demodulators=("USB", "LSB"),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "Ham RTTY (Baudot) carries no parity/FEC in this configuration; "
            "minimodem streams raw characters."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_backends.py"
                "::test_build_backend_rtty_minimodem_sets_idle_coalescer_and_preamble",
                "tests/decoder/test_backends.py::test_line_coalescer_emits_on_newline",
                "tests/decoder/test_backends.py::test_line_coalescer_flushes_after_idle",
                "tests/decoder/test_backends.py::test_line_coalescer_feed_line_emits_once",
            ],
            notes="LineCoalescer/backend-construction fixtures only; no off-air RTTY captured.",
        ),
        field_notes=(
            "Optional cross-check backend (minimodem's own baud/shift autodetect); not "
            "installed by default. Not verified off-air (HF); ships since 0.15.0 "
            "(2026-09-25), synthetic tests only."
        ),
    ),
    "dmr": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="live_output_observed",
            # dsd-fme's "-h" banner has no reliable version string (see readiness.py)
            backend_version=None,
            validated_on=date(2026, 10, 11),
            tests=[
                "tests/decoder/test_backends.py::test_parse_dsdfme_sync_line_updates_state_and_emits_nothing",
                "tests/decoder/test_backends.py::test_parse_dsdfme_tgt_src_line_emits_call_start",
                "tests/decoder/test_backends.py::test_parse_dsdfme_encrypted_variant_sets_flag",
                "tests/decoder/test_backends.py::test_parse_dsdfme_repeated_tgt_src_is_not_a_new_call",
                "tests/decoder/test_backends.py::test_parse_dsdfme_call_end_emits_duration_and_wav",
                "tests/decoder/test_backends.py::test_parse_dsdfme_call_end_without_wav_is_none",
                "tests/decoder/test_backends.py::test_parse_dsdfme_no_sync_without_active_call_is_none",
                "tests/decoder/test_backends.py::test_build_backend_dmr_argv",
                "tests/decoder/test_backends.py::test_build_backend_dsdfme_raises_for_missing_binary",
            ],
            notes=(
                "Live Capacity Plus control and talkgroup 70 calls were decoded at "
                "152.878017 MHz on 2026-10-03. dsd-fme saved non-silent call WAVs; "
                "the operator confirmed audible voice. Per-message integrity is not "
                "exposed, audio completeness and content have not been independently "
                "compared, and the backend version cannot be probed reliably. "
                "CI coverage remains synthetic parser fixtures. Live output "
                "reconfirmed by the operator 2026-10-11."
            ),
            report="docs/validation/live-vhf-uhf-2026-10-03.md",
        ),
        field_notes=(
            "Color code 02 observed in a private capture on 2026-09-25; "
            "live Capacity Plus control, call metadata, and operator-confirmed "
            "voice audio observed on 2026-10-03."
        ),
    ),
    "p25": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_dsdfme_p25_style_tg_src_line",
                "tests/decoder/test_backends.py::test_build_backend_p25_argv",
            ],
            notes=(
                "Synthetic dsd-fme stdout line fixtures only; no off-air P25 capture in this repo."
            ),
        ),
        field_notes="Not verified off-air; ships since 0.18.0 (2026-09-25), synthetic tests only.",
    ),
    "nxdn": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_SHIPPED_ON,
            tests=["tests/decoder/test_backends.py::test_build_backend_nxdn_argv"],
            notes=(
                "NXDN48 backend-construction test only; no confirmed off-air NXDN frame. "
                "The earlier sync-only attempt used dsd-fme auto mode, which does not "
                "detect NXDN, so it is not NXDN validation."
            ),
        ),
        field_notes=(
            "NXDN48 not verified off-air; explicit 6.25 kHz decoder mode "
            "requires a new field test."
        ),
    ),
    "nxdn96": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="live_output_observed",
            backend_version=None,
            validated_on=date(2026, 10, 3),
            tests=["tests/decoder/test_backends.py::test_build_backend_nxdn_argv"],
            notes=(
                "At 451.099600 MHz on 2026-10-03, off-air IQ produced dsd-fme "
                "NXDN96 RTCH2 data/voice indications and a Sync: NXDN96 line; "
                "the live app then reached synchronized. No call metadata, saved "
                "voice audio, or independent content comparison was obtained."
            ),
            report="docs/validation/live-vhf-uhf-2026-10-03.md",
        ),
        field_notes=(
            "Off-air NXDN96 synchronization observed at 451.099600 MHz; "
            "call content and voice remain unverified."
        ),
    ),
    "dstar": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_DSDFME_EXTRA_TESTED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_dsdfme_extra_modes_emit_sync_and_metadata",
                "tests/decoder/test_backends.py::test_build_backend_dsdfme_extra_mode_argv",
            ],
            notes="Synthetic dsd-fme parser and argv fixtures only; no off-air D-STAR recording.",
        ),
        field_notes="Flag checked in dsd-fme help; mode and call fields await off-air tests.",
    ),
    "ysf": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_DSDFME_EXTRA_TESTED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_dsdfme_extra_modes_emit_sync_and_metadata",
                "tests/decoder/test_backends.py::test_build_backend_dsdfme_extra_mode_argv",
            ],
            notes="Synthetic dsd-fme parser and argv fixtures only; no off-air YSF recording.",
        ),
        field_notes="Flag checked in dsd-fme help; mode and call fields await off-air tests.",
    ),
    "dpmr": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_DSDFME_EXTRA_TESTED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_dsdfme_extra_modes_emit_sync_and_metadata",
                "tests/decoder/test_backends.py::test_build_backend_dsdfme_extra_mode_argv",
            ],
            notes="Synthetic parser/argv fixtures only; dPMR polarity is not auto-detected.",
        ),
        field_notes=("Flag checked in dsd-fme help; fixed mode, polarity caveat, no off-air test."),
    ),
    "m17": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=_DSDFME_NO_FLAG,
        validation=Validation(
            state="fixture_tested",
            backend_version=None,
            validated_on=_DSDFME_EXTRA_TESTED_ON,
            tests=[
                "tests/decoder/test_backends.py::test_parse_dsdfme_extra_modes_emit_sync_and_metadata",
                "tests/decoder/test_backends.py::test_build_backend_dsdfme_extra_mode_argv",
            ],
            notes="Synthetic parser/argv fixtures only; M17 polarity is not auto-detected.",
        ),
        field_notes=("Flag checked in dsd-fme help; fixed mode, polarity caveat, no off-air test."),
    ),
    "wefax": CapabilityEntry(
        input_path="audio",
        demodulators=("USB",),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "Analog fax has no error-detection code; output is an image, checked only by eye."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_pydecoders.py::test_wefax_decodes_gradient_image_within_tolerance",
                "tests/decoder/test_pydecoders.py::test_wefax_writes_png_progressively_every_20_lines",
            ],
            notes="Synthesised-signal round trip only (own encoder -> own decoder).",
        ),
        field_notes="README: radiofax not verified off-air (no HF antenna connected).",
    ),
    "sstv": CapabilityEntry(
        input_path="audio",
        demodulators=("USB", "NFM"),
        single_channel=True,
        # "NFM" is in demodulators, so manager.start_band admits it; VIS
        # detection then runs per channel like any other NFM decoder.
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="none_exposed",
        integrity_notes=(
            "Analog SSTV has no error-detection code beyond the VIS parity bit checked once "
            "at picture start; output is an image, checked only by eye."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_SSTV_TESTED_ON,
            tests=[
                "tests/decoder/test_sstv.py::test_decodes_synthesised_image_with_noise",
                "tests/decoder/test_sstv.py::test_clock_offset_is_tracked_straight",
                "tests/decoder/test_sstv.py::test_dropout_yields_partial_image",
            ],
            notes=(
                "Synthesised-signal round trip only (own encoder -> own decoder) for "
                "Robot 36/72, Martin M1, Scottie S1, PD 120; other modes share the mode "
                "table but are untested."
            ),
        ),
        field_notes="Not verified off-air.",
    ),
    "navtex": CapabilityEntry(
        input_path="audio",
        demodulators=("USB",),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="fec",
        integrity_notes=(
            "SITOR-B 'B' (FEC) mode: each character is sent twice with a 4-character DX/RX "
            "delay; msdr.decoder.pydecoders.navtex._Fec compares the pair and substitutes the "
            "valid one (or '?' if both fail), and the per-message uncorrectable-character count "
            "is exposed as fields['errors']."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_pydecoders.py::test_navtex_decodes_clean_message_exactly",
                "tests/decoder/test_pydecoders.py::test_navtex_fec_delay_is_four_characters",
                "tests/decoder/test_pydecoders.py::test_navtex_repairs_one_corrupted_character_via_rx_repetition",
            ],
            notes="Synthesised-signal round trip only (own encoder -> own decoder).",
        ),
        field_notes="README: NAVTEX not verified off-air (no HF antenna connected).",
    ),
    "wwv": CapabilityEntry(
        input_path="audio",
        demodulators=("AM",),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="builtin_check",
        integrity_notes=(
            "Real WWV/WWVH carries no parity bits; msdr's own _decode_minute() (timecode.py) "
            "rejects a frame unless all marker positions read 'M' and the decoded "
            "minute/hour/day fall in range -- a plausibility gate msdr added, not a "
            "protocol-level check."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_timecode.py::test_wwv_decodes_one_minute_frame",
                "tests/decoder/test_timecode.py::test_wwv_ambiguous_pulse_widths_lower_quality",
                "tests/decoder/test_timecode.py::test_wwv_no_event_on_noise",
            ],
            notes=(
                "Synthesised-signal round trip only. README: 'WWV/WWVH frame layout is a "
                "reconstruction... not verified against an off-air recording'; station "
                "identification (500/600 Hz tone) is not implemented."
            ),
        ),
        field_notes="README: time-signal decoders not verified off-air (no HF antenna connected).",
    ),
    "dcf77": CapabilityEntry(
        input_path="audio",
        demodulators=("AM",),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="parity",
        integrity_notes=(
            "Real DCF77 even-parity bits (minute/hour/date) are computed and checked in "
            "_decode_minute() (timecode.py); the pass/fail result is folded into the emitted "
            "'quality' score (times 0.6 per failed check), not surfaced as its own boolean field."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_timecode.py::test_dcf77_decodes_one_minute_frame_cest_to_utc",
                "tests/decoder/test_timecode.py::test_dcf77_parity_error_lowers_quality_but_still_emits",
            ],
            notes="Synthesised-signal round trip only; not verified against an off-air recording.",
        ),
        field_notes="README: time-signal decoders not verified off-air (no HF antenna connected).",
    ),
    "msf": CapabilityEntry(
        input_path="audio",
        demodulators=("AM",),
        single_channel=True,
        band_path=False,
        replay_supported="planned_s3",
        integrity_policy="parity",
        integrity_notes=(
            "Real MSF (Anthorn) per-field parity bits (minute/hour/day+weekday/month+year) are "
            "computed and checked in _decode_minute() (timecode.py); folded into 'quality' the "
            "same way DCF77's are, not surfaced as its own boolean field."
        ),
        validation=Validation(
            state="fixture_tested",
            backend_version=_MSDR_VERSION,
            validated_on=_SHIPPED_ON,
            tests=[
                "tests/decoder/test_timecode.py::test_msf_decodes_one_minute_frame_bst_to_utc",
                "tests/decoder/test_timecode.py::test_msf_parity_error_lowers_quality_but_still_emits",
            ],
            notes=(
                "Synthesised-signal round trip only; README notes MSF's frame layout is "
                "'simplified rather than bit-exact to the published spec' in places."
            ),
        ),
        field_notes="README: time-signal decoders not verified off-air (no HF antenna connected).",
    ),
    "ais": CapabilityEntry(
        input_path="iq",
        demodulators=("NFM",),
        single_channel=True,
        band_path=True,
        replay_supported="planned_s3",
        integrity_policy="crc",
        integrity_notes=(
            "msdr.decoder.pydecoders.ais checks each HDLC frame's CRC-16/X.25 itself; a frame "
            "becomes a DecodeEvent only when the CRC matches and it is long enough to carry an "
            "MMSI. CRC failures are counted in AisDecoder.stats() (crc_fail), never emitted."
        ),
        validation=Validation(
            state="live_output_observed",
            backend_version=_MSDR_VERSION,
            validated_on=date(2026, 10, 11),
            tests=[
                "tests/decoder/test_ais.py::test_frame_success_by_ebn0",
                "tests/decoder/test_ais.py::test_noise_only_has_no_crc_false_passes",
                "tests/decoder/test_ais.py::test_type1_payload_fields_decode_exactly",
                "tests/decoder/test_ais.py::test_crc_failure_is_counted_not_emitted",
                "tests/decoder/test_ais.py::test_streamed_chunks_decode_every_burst_once",
                "tests/decoder/test_ais.py::test_decoder_is_input_scale_free",
            ],
            notes=(
                "Live off-air output confirmed by the operator (CRC-valid messages; "
                "reconfirmed 2026-10-11); independent comparison pending. Synthetic "
                "frame success, 200 synthetic bursts per point (random 168-bit payloads, "
                "+600 Hz offset, random phase/timing, 7.5 kHz channel filter, flat "
                "discriminator at 48 kHz), Eb/N0 10/12/14/16/20 dB: "
                "0.075 / 0.475 / 0.915 / 0.990 / 1.000 (a separate 1000-burst run at 14 dB "
                "measured 0.880; frame success also depends on payload content -- a fixed "
                "type-1 report with long zero runs measured 0.837). 0 CRC false passes on 10 s "
                "of noise-only input."
            ),
        ),
        field_notes=(
            "Live off-air AIS decoded with CRC-valid frames (operator-confirmed "
            "2026-10-11); no reference capture in tests/corpus yet "
            "(tests/corpus/manifest.json ais-161975-162025)."
        ),
    ),
}

for _decoder in ("adsb", "rs41", "ft8", "dsc", "endoftrain", "vor", "ils", "lora"):
    _checks_payload = _decoder in {"rs41", "endoftrain"}
    MATRIX[_decoder] = CapabilityEntry(
        input_path="iq",
        demodulators=("IQ",),
        single_channel=True,
        band_path=False,
        replay_supported="raw_iq",
        integrity_policy=(
            "crc"
            if _checks_payload or _decoder in {"ft8", "lora", "adsb"}
            else "builtin_check"
            if _decoder == "dsc"
            else "none_exposed"
        ),
        integrity_notes=(
            "Native IQ backend accepts RS41 sub-block CRC-16 or EOT CRC-18-valid frames only; "
            "RS41 applies RS(255,231) correction (up to 12 symbols per interleave lane) "
            "before payload CRC validation; synthetic only, no off-air validation."
            if _checks_payload
            else (
                "DSC verifies its XOR ECC."
                if _decoder == "dsc"
                else "WSJT-X jt9 accepts FT8 messages after LDPC/CRC checks."
                if _decoder == "ft8"
                else (
                    "msdr's native receiver (adsb_phy.py) checks each frame's CRC-24 "
                    "itself; a message becomes a DecodeEvent only on an exact match -- "
                    "no bit-flip correction is attempted (ported from SDRangel's ADS-B "
                    "demod, which does not correct ADS-B CRC errors either). The "
                    "alternative dump1090-fa backend's BaseStation output exposes no "
                    "per-message CRC status."
                )
                if _decoder == "adsb"
                else (
                    "msdr's native receiver (lora_phy.py) checks the payload CRC-16 "
                    "itself; a frame becomes a DecodeEvent only when crc_valid is "
                    "true. The optional gr-lora_sdr cross-check backend (GrLoRaBackend) "
                    "instead relies on its own frame_info CRC tag."
                )
                if _decoder == "lora"
                else (
                    "VOR/ILS radial/DDM are unvalidated measurements, with no integrity "
                    "check. The Morse station ident (ported from SDRangel's MorseDemod) "
                    "is only exposed once decoded identically twice in a row; synthetic "
                    "7 wpm idents (tests/decoder/test_navigation_ident.py) decode "
                    "reliably down to about -5 dB SNR, and correctly report no ident "
                    "(never a wrong one) at -9 dB and below."
                )
            )
        ),
        validation=Validation(
            state="fixture_tested" if _checks_payload or _decoder in {"vor", "ils"} else "untested",
            tests=(
                ["tests/decoder/test_payloads.py"]
                if _checks_payload
                else [
                    "tests/decoder/test_navigation_ident.py",
                    "tests/decoder/test_navigation_native.py",
                ]
                if _decoder in {"vor", "ils"}
                else []
            ),
            notes=(
                "Ident decode validated against synthetic 7 wpm Morse only; VOR/ILS "
                "radial/DDM measurement itself and off-air ident validation are both "
                "still pending."
                if _decoder in {"vor", "ils"}
                else "Payload parser checks only; reference IQ and off-air validation pending."
            ),
        ),
        field_notes=(
            "Native numpy receiver, builtin; optional gr-lora_sdr cross-check backend "
            "(GrLoRaBackend). Live and recording IQ input. RF validation is pending; "
            if _decoder == "lora"
            else "Native numpy receiver, builtin; dump1090-fa remains available as an "
            "alternative backend (standalone_adsb.py). Live and recording IQ input. "
            "RF validation is pending; "
            if _decoder == "adsb"
            else "Standalone decoder; live and recording IQ input. RF validation is pending; "
        )
        + "not supported by the NFM band channelizer; replay uses raw IQ, not audio taps.",
    )

# Native ADS-B/Mode S receiver: validated against published Mode S/ADS-B
# reference frames from "The 1090 MHz Riddle" (Junzi Sun, MIT-licensed).
# Synthetic-IQ SNR/throughput numbers only below -- no off-air capture
# validated yet.
MATRIX["adsb"] = replace(
    MATRIX["adsb"],
    validation=Validation(
        state="live_output_observed",
        backend_version=_MSDR_VERSION,
        validated_on=date(2026, 10, 11),
        tests=["tests/decoder/test_adsb_native.py"],
        report="docs/adsb-native.md",
        notes=(
            "Live off-air output confirmed by the operator 2026-10-11 (aircraft "
            "reports and map positions); independent same-IQ comparison pending. "
            "Reference IQ (2026-10-03): 4/4 published reference frames "
            "(identification, airborne position pair, "
            "velocity) decode bit-exactly through the full preamble+PPM demod pipeline "
            "with a valid CRC-24, including the published callsign (KLM1023), "
            "CPR-resolved position (52.25720214843750 N, 3.91937255859375 E) and "
            "velocity (159.2 kt, 182.88 deg track); single-bit corruption is rejected, "
            "never silently corrected. Synthetic PPM bursts, 100 trials/point: DF17 "
            "message success vs SNR (2 MS/s) 0%/64%/100%/100%/100%/100% at "
            "6/10/14/18/22/26 dB; DF11 0%/81%/100%/100%/100%/100%. Overlapping/close "
            "bursts and the 2.4 MS/s resample path (msdr's own rate, oversampled to "
            "2 samples/chip rather than decimated, to avoid landing on a bad chip "
            "phase) both decode correctly. Real-time factor at 2 MS/s on the "
            "authoring Mac: 0.016 (50 bursts/s). Scope: DF17/18 and DF11 only; other "
            "Mode S downlink formats and Gillham-coded (Q=0) altitude are not decoded."
        ),
    ),
)

# Native LoRa receiver: validated against real gr-lora_sdr TX reference IQ
# (tests/decoder/data/lora/, GNU Radio 3.10.11 + gr-lora_sdr commit
# fbbf6ea6684d, the build whose receive chain the bit conventions follow).
# All 20 reference_decode=true cases (SF7-SF12; CR 4/5-4/8; 125/250/500 kHz;
# LDRO on/off; explicit and implicit header; CRC on/off; 16-200 byte
# payloads; Meshtastic LongFast sync 0x2B) decode bit-exactly with a valid
# CRC. Off-air validation is still pending.
MATRIX["lora"] = replace(
    MATRIX["lora"],
    validation=Validation(
        state="live_output_observed",
        backend_version=_MSDR_VERSION,
        validated_on=date(2026, 10, 11),
        tests=[
            "tests/decoder/test_lora_phy.py",
            "tests/decoder/test_lora_native.py",
            "tests/decoder/test_lora.py",
        ],
        fixtures=["tests/decoder/data/lora/vectors.npz"],
        report="docs/lora-native.md",
        notes=(
            "Live off-air MeshCore packets decoded with a valid CRC, operator-confirmed "
            "2026-10-11; no off-air reference comparison yet. Reference IQ "
            "(2026-10-03): 20/20 real gr-lora_sdr reference_decode=true vectors decode "
            "to the exact "
            "payload with a valid CRC (SF7-SF12, all coding rates, 125/250/500 kHz, LDRO, "
            "implicit header, CRC off, Meshtastic LongFast). AWGN on the real vectors "
            "(SNR per sample in the channel bandwidth, measured over the packet): "
            "SF7/125 kHz 100% to -7 dB, 80% at -9 dB (20 trials); SF11/250 kHz "
            "LongFast 100% to -15 dB (10 trials). Real-time factor at 2.4 MS/s on the "
            "authoring Mac: 0.10 at SF7 and SF12. No parity claim versus gr-lora_sdr."
        ),
    ),
)

# Native FT8 runtime checked with WSJT-X 3.0.2 and a recorded audio fixture.
MATRIX["ft8"] = replace(
    MATRIX["ft8"],
    validation=Validation(
        state="reference_iq_tested",
        backend_version="3.0.2",
        validated_on=date(2026, 9, 27),
        tests=["tests/decoder/test_standalone_ft8.py"],
        fixtures=["SDRangel sdrbench/samples/ft8/230105_091630.wav"],
        report="docs/ft8.md",
        notes="Native jt9 decoded 13 recorded-audio messages; synthetic 48 kHz IQ "
        "passed msdr channelization and decoding. Live UTC alignment remains unvalidated.",
    ),
)

MATRIX["dsc"] = replace(
    MATRIX["dsc"],
    validation=Validation(
        state="fixture_tested",
        backend_version=_MSDR_VERSION,
        validated_on=date(2026, 9, 27),
        tests=["tests/decoder/test_dsc_semantics.py"],
        report="docs/dsc.md",
        notes="Synthetic HF 100-baud and VHF 1200-baud IQ decode checks plus conservative "
        "message parsing. Independent off-air validation is pending.",
    ),
)

# -- RDS (msdr/survey/rds.py) -- tracked separately: it's a survey-side FM
# broadcast decoder over the wideband IQ path, not a DECODERS/manager entry
# (no ProcessBackend, no manager channel, no band.py slot).
RDS_ENTRY = CapabilityEntry(
    input_path="iq",
    demodulators=("WFM",),
    single_channel=False,
    band_path=False,
    replay_supported="planned_s3",
    integrity_policy="crc",
    integrity_notes=(
        "RDS groups use a (26,16) shortened cyclic block code (g(x)=x^10+x^8+x^7+x^5+x^4+x^3+1) "
        "with A/B/C/C'/D offset words; msdr.survey.rds verifies each block's syndrome, corrects "
        "1-2 bit bursts, and only holds block sync while offset-word hits keep matching -- a real "
        "CRC-style check, not a passthrough."
    ),
    validation=Validation(
        state="fixture_tested",
        backend_version=_MSDR_VERSION,
        validated_on=date(2026, 9, 26),
        tests=[
            "tests/survey/test_rds.py::test_decodes_ps_pi_rt_from_clean_iq",
            "tests/survey/test_rds.py::test_mono_station_without_pilot_decodes",
            "tests/survey/test_rds.py::test_block_code_offsets_are_syndromes",
            "tests/survey/test_rds.py::test_single_bit_errors_are_corrected",
            "tests/survey/test_rds.py::test_heavy_burst_errors_never_yield_wrong_ps",
        ],
        fixtures=[],
        notes=(
            "Synthetic encoder fixtures only (encode_rds_mpx -> FM IQ -> WfmChannel); "
            "off-air captures are kept out of the repository. Decodes are checkword-verified "
            "block-code results, not station-identity claims -- see "
            "tests/corpus/manifest.json for the operator-reported station list, which "
            "is unverified claims, not ground truth."
        ),
    ),
    field_notes=(
        "KNBR 104.5 (mono, no 19 kHz pilot) decoded live 2026-09-26 via the free-running 57 kHz "
        "fallback path (see msdr/survey/rds.py module docstring); other FM-band station identity "
        "claims in tests/corpus/manifest.json are operator reports, not decoded/confirmed."
    ),
)

# Off-air results (2026-10-11), see docs/validation/operator-checklist.md
# G07/G15. Built-in, so versioned with msdr like the entries above.
MATRIX["endoftrain"] = replace(
    MATRIX["endoftrain"],
    validation=Validation(
        state="live_output_observed",
        backend_version=_MSDR_VERSION,
        validated_on=date(2026, 10, 11),
        tests=["tests/decoder/test_payloads.py"],
        notes=(
            "Live off-air packets decoded with matching CRC-18 and plausible fields "
            "(rear brake-pipe pressure 79 psig on a stopped train, marker light on, "
            "valve circuit ok). Independent reference comparison pending."
        ),
    ),
)
MATRIX["vor"] = replace(
    MATRIX["vor"],
    validation=Validation(
        state="field_observed",
        backend_version=_MSDR_VERSION,
        validated_on=date(2026, 10, 11),
        tests=["tests/decoder/test_navigation_native.py", "tests/decoder/test_navigation_ident.py"],
        notes=(
            "Off-air against OAK VOR from a known location: radials 265.8-269.7 deg vs "
            "267-271 deg computed bearing, subcarrier deviation 477-485 Hz (standard "
            "480 Hz). This check exposed and verified the fix for a sign error "
            "(previously 360 deg minus the radial). ILS remains fixture-tested only."
        ),
    ),
)
