import asyncio
import os
import pathlib
import sys
import time

import pytest

from msdr.decoder.backends import (
    DecodeEvent,
    DecoderError,
    DsdState,
    LineCoalescer,
    ProcessBackend,
    PythonBackend,
    build_backend,
    parse_direwolf_line,
    parse_dsdfme_line,
    parse_multimon_json,
    parse_multimon_plain,
)
from msdr.decoder.registry import DECODERS

# Sample line from the spec ("Measured facts"): multimon-ng --json POCSAG output.
POCSAG_JSON_LINE = (
    '{"demod_name":"POCSAG1200","address":100001,"function":2,"alpha":"ALPHA - Test Page #00042"}'
)


def test_parse_multimon_json_pocsag_sample():
    event = parse_multimon_json(POCSAG_JSON_LINE, "pocsag")
    assert event is not None
    assert event.decoder == "pocsag"
    assert event.kind == "text"
    assert event.text == "ALPHA - Test Page #00042"
    assert event.fields == {"address": 100001, "function": 2, "demod": "POCSAG1200"}


def test_parse_multimon_json_prefers_numeric_then_message():
    event = parse_multimon_json('{"demod_name":"EAS","message":"hello"}', "eas")
    assert event.text == "hello"
    assert event.fields == {"demod": "EAS"}


def test_parse_multimon_json_ignores_blank_and_bad_lines():
    assert parse_multimon_json("", "pocsag") is None
    assert parse_multimon_json("not json", "pocsag") is None


def test_parse_multimon_json_drops_empty_tone_only_page():
    # -e (registry.py) hides these at the multimon-ng level already, but the
    # parser drops them too in case a build without -e ever slips through:
    # no text and no address/tgt/src means nothing worth storing.
    line = '{"demod_name":"POCSAG1200","function":2}'
    assert parse_multimon_json(line, "pocsag") is None


def test_parse_multimon_json_keeps_empty_text_with_address():
    # An address-only page (no alpha/numeric/message) still carries an
    # identifiable target, so it's kept even though text=="".
    line = '{"demod_name":"POCSAG1200","address":100001,"function":2}'
    event = parse_multimon_json(line, "pocsag")
    assert event is not None
    assert event.text == ""
    assert event.fields["address"] == 100001


def test_parse_multimon_plain_aprs_style():
    event = parse_multimon_plain("AFSK1200: WX9ABC>APRS,TCPIP*:hello world", "aprs")
    assert event.text == "WX9ABC>APRS,TCPIP*:hello world"
    assert event.fields == {
        "demod": "AFSK1200",
        "from": "WX9ABC",
        "to": "APRS",
        "path": ["TCPIP*"],
    }


def test_parse_multimon_plain_drops_empty_text_no_payload():
    assert parse_multimon_plain("MORSE_CW: ", "cw") is None


def test_parse_multimon_plain_without_colon_is_none():
    assert parse_multimon_plain("garbage line", "cw") is None


def test_parse_multimon_plain_cw_has_no_tnc2_fields():
    # MORSE_CW text has no "SRC>DEST:" shape, so only "demod" is present.
    event = parse_multimon_plain("MORSE_CW: CQ CQ DE W1AW", "cw")
    assert event.fields == {"demod": "MORSE_CW"}


def test_parse_multimon_plain_multi_hop_path():
    event = parse_multimon_plain("FSK9600: W1AW-1>APRS,WIDE1-1,WIDE2-1:hello", "packet9600")
    assert event.fields == {
        "demod": "FSK9600",
        "from": "W1AW-1",
        "to": "APRS",
        "path": ["WIDE1-1", "WIDE2-1"],
    }


# -- direwolf monitor-format lines ------------------------------------------


def test_parse_direwolf_line_sample():
    line = "[0.3] W1AW-1>APRS,WIDE1-1,WIDE2-1:!4217.00N/07154.00W-Test"
    event = parse_direwolf_line(line, "aprs")
    assert event is not None
    assert event.decoder == "aprs"
    assert event.text == "W1AW-1>APRS,WIDE1-1,WIDE2-1:!4217.00N/07154.00W-Test"
    assert event.fields == {
        "from": "W1AW-1",
        "to": "APRS",
        "path": ["WIDE1-1", "WIDE2-1"],
        "demod": "direwolf",
    }


def test_parse_direwolf_line_ignores_chatter():
    assert parse_direwolf_line("Dire Wolf version 1.7", "aprs") is None


def test_parse_direwolf_line_ignores_transmit_echo():
    # "[0L]" (Locally originated) is direwolf's own transmit echo, not a
    # received frame -- excluded because "L" isn't a subchannel number.
    assert parse_direwolf_line("[0L] W1AW-1>APRS:test", "aprs") is None


def test_parse_direwolf_line_ignores_blank():
    assert parse_direwolf_line("", "aprs") is None


class _FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_line_coalescer_emits_on_newline():
    clock = _FakeClock()
    coalescer = LineCoalescer("rtty", idle_s=2.0, clock=clock)
    for ch in "HELLO":
        assert coalescer.feed_char(ch) is None
    event = coalescer.feed_char("\n")
    assert event is not None
    assert event.text == "HELLO"
    assert event.decoder == "rtty"


def test_line_coalescer_flushes_after_idle():
    clock = _FakeClock()
    coalescer = LineCoalescer("rtty", idle_s=2.0, clock=clock)
    for ch in "RST 599":
        coalescer.feed_char(ch)
    assert coalescer.flush() is None  # no time has passed yet
    clock.t += 2.5
    event = coalescer.flush()
    assert event is not None
    assert event.text == "RST 599"


def test_line_coalescer_feed_line_emits_once():
    coalescer = LineCoalescer("rtty")
    event = coalescer.feed_line("CQ CQ DE W1AW")
    assert event.text == "CQ CQ DE W1AW"


def test_line_coalescer_empty_flush_is_none():
    coalescer = LineCoalescer("rtty")
    assert coalescer.flush() is None


@pytest.mark.asyncio
async def test_process_backend_echo_child_round_trip():
    """No real decoder binary: a python -c echo child stands in for one."""
    echo_argv = [
        sys.executable,
        "-u",
        "-c",
        "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line)\n    sys.stdout.flush()\n",
    ]
    seen = []

    def parser(line: str):
        seen.append(line)
        return parse_multimon_plain(f"ECHO: {line}", "echo") if line else None

    backend = ProcessBackend(echo_argv, parser, decoder_id="echo")
    await backend.start()
    try:
        backend.feed(b"hello world\n")
        events_iter = backend.events()
        event = await events_iter.__anext__()
        assert event.text == "hello world"
        assert event.decoder == "echo"
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_process_backend_emits_new_dsdfme_event_log_calls(tmp_path):
    log_path = tmp_path / "nxdn96-events.log"
    log_path.write_text(
        "2026-10-03 18:00:00 NXDN TGT: 00000111; SRC: 00000111; RAN: 07; Group;\n"
    )
    state = DsdState()
    backend = ProcessBackend(
        [sys.executable, "-u", "-c", "import sys; sys.stdin.read()"],
        lambda line: parse_dsdfme_line(line, "nxdn96", state),
        decoder_id="nxdn96",
        event_log_path=log_path,
    )
    await backend.start()
    try:
        with log_path.open("ab") as log:
            log.write(b"2026-10-03 18:36:31 NXDN TGT: 00000777; SRC: ")
            log.flush()
        await asyncio.sleep(0.15)
        assert backend._events.empty()  # neither stale nor partial call
        with log_path.open("ab") as log:
            log.write(b"00000777; RAN: 07; Group;\n")
        event = await asyncio.wait_for(backend.events().__anext__(), timeout=1)
        assert event.fields["tgt"] == "00000777"
        assert event.fields["ran"] == "07"
        assert backend._events.empty()
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_process_backend_removes_header_only_temporary_event_log(tmp_path):
    log_path = tmp_path / "nxdn96-events.log"
    log_path.write_text(
        "2026-10-03 18:00:00 DSD-FME Started and Event History Initialized;\n"
        "2026-10-03 18:00:00 Any decoded voice calls or data calls display here;\n"
    )
    backend = ProcessBackend(
        [sys.executable, "-u", "-c", "import sys; sys.stdin.read()"],
        lambda line: None,
        event_log_path=log_path,
        cleanup_empty_event_log=True,
    )
    await backend.start()
    await backend.stop()
    assert not log_path.exists()


@pytest.mark.asyncio
async def test_process_backend_error_after_exit():
    # A child that exits immediately with a stderr message.
    argv = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(1)"]
    backend = ProcessBackend(argv, lambda line: None, decoder_id="dying")
    await backend.start()
    # Give the child a moment to exit and stderr to be read.
    for _ in range(50):
        if backend.error is not None:
            break
        await asyncio.sleep(0.05)
    assert backend.error == "boom"
    await backend.stop()


def test_build_backend_raises_for_missing_binary():
    spec = DECODERS["pocsag"]
    with pytest.raises(DecoderError):
        build_backend(spec, {"multimon-ng": None})


def test_build_backend_rtty_minimodem_sets_idle_coalescer_and_preamble():
    spec = DECODERS["rtty_minimodem"]
    backend = build_backend(spec, {"minimodem": "/usr/local/bin/minimodem"})
    assert backend.decoder_id == "rtty_minimodem"
    assert hasattr(backend, "idle_coalescer")
    assert backend._preamble.startswith(b"RIFF")


def test_build_backend_pocsag_uses_json_parser():
    spec = DECODERS["pocsag"]
    backend = build_backend(spec, {"multimon-ng": "/usr/local/bin/multimon-ng"})
    event = backend._parser(POCSAG_JSON_LINE)
    assert event.text == "ALPHA - Test Page #00042"


def test_pocsag_argv_hides_empty_messages():
    spec = DECODERS["pocsag"]
    backend = build_backend(spec, {"multimon-ng": "/usr/local/bin/multimon-ng"})
    assert "-e" in backend._argv


def test_build_backend_aprs_uses_plain_parser():
    spec = DECODERS["aprs"]
    backend = build_backend(spec, {"multimon-ng": "/usr/local/bin/multimon-ng"})
    event = backend._parser("AFSK1200: hello")
    assert event.fields == {"demod": "AFSK1200"}


_BOTH_BINARIES = {
    "multimon-ng": "/usr/local/bin/multimon-ng",
    "direwolf": "/opt/homebrew/bin/direwolf",
}


def test_build_backend_aprs_prefers_direwolf_when_present():
    spec = DECODERS["aprs"]
    backend = build_backend(spec, _BOTH_BINARIES)
    assert backend._argv[0] == "/opt/homebrew/bin/direwolf"
    assert "-B" in backend._argv and "1200" in backend._argv
    assert backend._argv[-1] == "-"


def test_build_backend_aprs_uses_multimon_without_direwolf():
    spec = DECODERS["aprs"]
    backend = build_backend(spec, {"multimon-ng": "/usr/local/bin/multimon-ng", "direwolf": None})
    assert backend._argv[0] == "/usr/local/bin/multimon-ng"


def test_build_backend_packet9600_prefers_direwolf_with_9600_baud():
    spec = DECODERS["packet9600"]
    backend = build_backend(spec, _BOTH_BINARIES)
    assert backend._argv[0] == "/opt/homebrew/bin/direwolf"
    assert "9600" in backend._argv


def test_build_backend_packet300_requires_direwolf():
    spec = DECODERS["packet300"]
    with pytest.raises(DecoderError):
        build_backend(spec, {"direwolf": None})
    backend = build_backend(spec, {"direwolf": "/opt/homebrew/bin/direwolf"})
    assert backend._argv[1] == "-c"
    assert pathlib.Path(backend._argv[2]).read_text().splitlines()[1:] == [
        "AGWPORT 0",
        "KISSPORT 0",
    ]
    assert backend._argv[:1] + backend._argv[3:] == [
        "/opt/homebrew/bin/direwolf",
        "-n",
        "1",
        "-r",
        "22050",
        "-b",
        "16",
        "-t",
        "0",
        "-q",
        "hd",
        "-B",
        "300",
        "-",
    ]


# -- dsd-fme (DMR/P25/NXDN) ----------------------------------------------

# Sample line from the plan's "Measured facts" (ANSI colour code trailing).
_SYNC_LINE = "20:26:11 Sync: +DMR  [slot1]  slot2  | Color Code=02 | CSBK\x1b[33m"


def test_parse_dsdfme_sync_line_updates_state_and_emits_nothing():
    state = DsdState()
    event = parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    assert event is None
    assert state.protocol == "DMR"
    assert state.colour == "02"
    assert state.slot == 1


def test_parse_dsdfme_capacity_plus_status_changes_without_burst_duplicates():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    header = " Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 2 - Single Block"
    slots = "  LSN 01:  Idle;  LSN 02:  Rest;  LSN 03:  Idle;  LSN 04:  Idle;"
    assert parse_dsdfme_line(header, "dmr", state) is None
    event = parse_dsdfme_line(slots, "dmr", state)
    assert event.kind == "control"
    assert event.fields["message_type"] == "channel_status"
    assert event.fields["rest_lsn"] == 2
    assert event.fields["lsn_status"] == {
        "1": "Idle",
        "2": "Rest",
        "3": "Idle",
        "4": "Idle",
    }
    assert event.integrity == "unavailable"
    assert "TG" not in event.text
    assert state.current is None

    # The same channel map repeats on alternating timeslots throughout the
    # measured log. A TS change alone is not a new control observation.
    parse_dsdfme_line(header.replace("TS: 1", "TS: 0"), "dmr", state)
    assert parse_dsdfme_line(slots, "dmr", state) is None

    parse_dsdfme_line(header.replace("Rest LSN: 2", "Rest LSN: 4"), "dmr", state)
    changed = parse_dsdfme_line(
        "LSN 01: Idle; LSN 02: Idle; LSN 03: 70; LSN 04: Rest;", "dmr", state
    )
    assert changed.fields["rest_lsn"] == 4
    assert changed.fields["lsn_status"]["3"] == "70"
    assert changed.fields["lsn_status"]["4"] == "Rest"


def test_parse_dsdfme_capacity_plus_site_status_and_crc_error():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    site_line = "\x1b[33m SLCO Capacity Plus Site: 1 - Rest LSN: 3 - RS: 00\x1b[0m"
    event = parse_dsdfme_line(site_line, "dmr", state)
    assert event.kind == "control"
    assert event.fields["message_type"] == "site_status"
    assert event.fields["site"] == 1
    assert event.fields["rest_lsn"] == 3
    assert event.integrity == "unavailable"
    assert parse_dsdfme_line(site_line, "dmr", state) is None
    assert parse_dsdfme_line("SLCO CRC ERR", "dmr", state) is None
    assert state.cap_plus_site == (1, 3, 0)


def test_parse_dsdfme_capacity_plus_rejects_error_marked_control_lines():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    header = "Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 2 - Single Block"
    slots = "LSN 01: Idle; LSN 02: Rest;"
    assert parse_dsdfme_line(header + " CRC ERR", "dmr", state) is None
    assert parse_dsdfme_line(slots, "dmr", state) is None
    assert parse_dsdfme_line(header, "dmr", state) is None
    assert parse_dsdfme_line(slots + " FEC ERR", "dmr", state) is None
    assert (
        parse_dsdfme_line("SLCO Capacity Plus Site: 1 - Rest LSN: 2 - RS: 00 CRC ERR", "dmr", state)
        is None
    )
    assert state.cap_plus_status is None
    assert state.cap_plus_site is None

    parse_dsdfme_line("Sync: +DMR | Color Code=02 | CSBK CRC ERR", "dmr", state)
    assert parse_dsdfme_line(header, "dmr", state) is None
    assert parse_dsdfme_line(slots, "dmr", state) is None
    assert state.cap_plus_status is None


def test_parse_dsdfme_capacity_plus_discards_context_after_no_sync_and_color_change():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    site_line = "SLCO Capacity Plus Site: 1 - Rest LSN: 2 - RS: 00"
    assert parse_dsdfme_line(site_line, "dmr", state) is not None
    parse_dsdfme_line("Sync: no sync", "dmr", state)
    assert state.colour is None
    assert state.cap_plus_site is None
    assert parse_dsdfme_line(site_line, "dmr", state) is None

    parse_dsdfme_line(_SYNC_LINE.replace("Code=02", "Code=03"), "dmr", state)
    event = parse_dsdfme_line(site_line, "dmr", state)
    assert event.fields["color_code"] == "03"
    assert event.fields["site"] == 1


def test_parse_dsdfme_capacity_plus_is_dmr_only_and_no_sync_is_not_a_protocol():
    state = DsdState()
    assert parse_dsdfme_line("Sync: no sync", "dmr", state) is None
    assert state.synchronized is False
    assert state.protocol is None
    line = "Capacity Plus Channel Status - FL: 3 TS: 1 RS: 0 - Rest LSN: 2 - Single Block"
    assert parse_dsdfme_line(line, "p25", DsdState()) is None


def test_parse_dsdfme_banner_lines_ignored():
    state = DsdState()
    banner = "──── DSD-FME " + ("█" * 10)
    assert parse_dsdfme_line(banner, "dmr", state) is None
    # Lines from other box/block glyphs (no ─ or ▟) must also be dropped (0.69.1).
    for glyph in ("█", "▄", "┃", "━", "▓"):
        assert parse_dsdfme_line(glyph * 12, "dmr", state) is None
    assert parse_dsdfme_line("", "dmr", state) is None
    assert state.protocol is None


def test_parse_dsdfme_tgt_src_line_emits_call_start():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    event = parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    assert event is not None
    assert event.kind == "call"
    assert event.decoder == "dmr"
    assert event.fields == {
        "protocol": "DMR",
        "tgt": "1234",
        "src": "5678",
        "color_code": "02",
        "slot": 1,
        "encrypted": False,
        "wav": None,
    }
    assert "1234" in event.text and "5678" in event.text


def test_parse_dsdfme_timestamped_nxdn_event_log_line():
    event = parse_dsdfme_line(
        "2026-10-03 18:35:05 NXDN TGT: 00000777; SRC: 00000777; "
        "RAN: 07; ENC; ALG: 1; KID: 00; Group;",
        "nxdn96",
        DsdState(),
    )
    assert event is not None
    assert event.kind == "call"
    assert event.fields["tgt"] == "00000777"
    assert event.fields["ran"] == "07"
    assert event.fields["encrypted"] is True


def test_parse_dsdfme_rejects_status_label_as_nxdn_call():
    state = DsdState()
    state.protocol = "IDAS"
    assert parse_dsdfme_line("Dst/TG /TG SRC 0", "nxdn", state) is None
    assert state.current is None


def test_parse_dsdfme_idas_event_log_retains_ran():
    state = DsdState(protocol="IDAS")
    event = parse_dsdfme_line(
        "2026-10-03 18:35:05 NXDN TGT: 00000777; SRC: 00000777; RAN: 07; Group;",
        "nxdn",
        state,
    )
    assert event is not None
    assert event.fields["ran"] == "07"


def test_parse_dsdfme_encrypted_variant_sets_flag():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    event = parse_dsdfme_line("TGT=1234 SRC=5678 ALG=AES", "dmr", state)
    assert event.fields["encrypted"] is True


def test_parse_dsdfme_repeated_tgt_src_is_not_a_new_call():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    first = parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    assert first is not None
    second = parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    assert second is None  # same call continuing, no duplicate event


def test_parse_dsdfme_p25_style_tg_src_line():
    state = DsdState()
    state.protocol = "P25"
    state.colour = "1a3"
    event = parse_dsdfme_line("TG 100 SRC 200", "p25", state)
    assert event.fields["tgt"] == "100"
    assert event.fields["src"] == "200"
    assert event.fields["nac"] == "1a3"


@pytest.mark.parametrize(
    ("decoder_id", "sync_line", "metadata_line", "field", "value"),
    [
        (
            "dstar",
            "Sync: +DSTAR",
            "RPT1=REF001",
            "repeater_1",
            "REF001",
        ),
        (
            "ysf",
            "Sync: +YSF",
            "TG=YSF123 SRC=K1ABC",
            "src",
            "K1ABC",
        ),
        (
            "dpmr",
            "Sync: +dPMR DPMR_CC_3",
            "DST=123 SRC=456",
            "color_code",
            "3",
        ),
        (
            "m17",
            "Sync: +M17 STR CAN=4",
            "Src=K1ABC - Dst/TG=ALL",
            "can",
            "4",
        ),
    ],
)
def test_parse_dsdfme_extra_modes_emit_sync_and_metadata(
    decoder_id, sync_line, metadata_line, field, value
):
    state = DsdState()
    sync = parse_dsdfme_line(sync_line, decoder_id, state)
    assert sync is not None
    assert sync.kind == "sync"
    assert sync.fields["synchronized"] is True
    assert sync.integrity == "unavailable"
    assert parse_dsdfme_line(sync_line, decoder_id, state) is None  # no burst spam

    output = parse_dsdfme_line(metadata_line, decoder_id, state)
    assert output is not None
    if decoder_id in ("ysf", "dpmr", "m17"):
        # Both endpoint identifiers create a call event, which can link the
        # .wav produced by dsd-fme's -P option when the call ends.
        assert output.kind == "call"
        assert output.fields["src"] == ("K1ABC" if decoder_id != "dpmr" else "456")
    else:
        assert output.kind == "metadata"
    assert output.fields[field] == value
    assert output.integrity == "unavailable"


def test_dsdfme_dstar_header_fields_create_call_when_both_endpoints_known():
    state = DsdState()
    parse_dsdfme_line("Sync: +DSTAR", "dstar", state)
    parse_dsdfme_line("MYCALL=K1ABC", "dstar", state)
    event = parse_dsdfme_line("URCALL=CQCQCQ", "dstar", state)
    assert event.kind == "call"
    assert event.fields["src"] == "K1ABC"
    assert event.fields["tgt"] == "CQCQCQ"
    assert event.fields["destination"] == "CQCQCQ"


def test_parse_dsdfme_call_end_emits_duration_and_wav(tmp_path):
    calls_dir = tmp_path
    state = DsdState(calls_dir=calls_dir)
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    start_event = parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    assert start_event is not None
    started_at = state.started_at

    wav_path = calls_dir / "20260925_162611_12345_DMR_CC_2_GROUP_TGT_1234_SRC_5678.wav"
    wav_path.write_bytes(b"RIFF....WAVEfmt ")
    os.utime(wav_path, (started_at + 1, started_at + 1))

    end_event = parse_dsdfme_line("20:26:20 No Sync", "dmr", state)
    assert end_event is not None
    assert end_event.kind == "call"
    assert end_event.fields["duration_s"] >= 0
    assert end_event.fields["wav"] == str(wav_path)
    assert state.current is None


def test_parse_dsdfme_call_wav_matches_tgt_src_not_newest(tmp_path):
    # Newest-file matching linked a 9 s call to a 0 s TGT_0_SRC_0 placeholder
    # and to other slots'/channels' files sharing calls_dir.
    state = DsdState(calls_dir=tmp_path)
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    later = state.started_at + 1
    ours = tmp_path / "20260925_162611_11111_DMR_CC_2_GROUP_TGT_1234_SRC_5678.wav"
    for name in (
        ours.name,
        "19691231_160000_22222__GROUP_TGT_0_SRC_0.wav",
        "20260925_162612_33333_DMR_CC_2_GROUP_TGT_95_SRC_9001.wav",
    ):
        (tmp_path / name).write_bytes(b"RIFF")
        os.utime(tmp_path / name, (later, later))
    os.utime(ours, (later - 0.5, later - 0.5))  # placeholder/others are newer
    end_event = parse_dsdfme_line("No Sync", "dmr", state)
    assert end_event.fields["wav"] == str(ours)


def test_parse_dsdfme_two_slots_do_not_fragment_each_other():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    events = []
    for line in [
        "DMR TGT: 00000070; SRC: 00005015; CC: 02; Group; Slot 1;",
        "DMR TGT: 00000095; SRC: 00009001; CC: 02; Group; Slot 2;",
        "TGT=70 SRC=5015",  # stderr spelling of the slot-1 call
        "DMR TGT: 00000095; SRC: 00009001; CC: 02; Group; Slot 2;",
        "DMR TGT: 00000070; SRC: 00005015; CC: 02; Group; Slot 1;",
        "No Sync",
        "x",  # drains the second queued end event
    ]:
        event = parse_dsdfme_line(line, "dmr", state)
        if event is not None:
            events.append(event)
    starts = [e for e in events if "duration_s" not in e.fields]
    ends = [e for e in events if "duration_s" in e.fields]
    assert [(e.fields["tgt"], e.fields["slot"]) for e in starts] == [
        ("00000070", 1),
        ("00000095", 2),
    ]
    assert sorted(e.fields["src"] for e in ends) == ["00005015", "00009001"]
    assert state.current is None


def test_parse_dsdfme_new_call_on_same_slot_ends_previous():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    end = parse_dsdfme_line("TGT=1234 SRC=9999", "dmr", state)
    assert end.fields["src"] == "5678" and "duration_s" in end.fields
    start = parse_dsdfme_line("Sync: +DMR  [slot1]  slot2  | Color Code=02", "dmr", state)
    assert start.fields["src"] == "9999" and "duration_s" not in start.fields


def test_parse_dsdfme_call_end_without_wav_is_none():
    state = DsdState()
    parse_dsdfme_line(_SYNC_LINE, "dmr", state)
    parse_dsdfme_line("TGT=1234 SRC=5678", "dmr", state)
    end_event = parse_dsdfme_line("No Sync", "dmr", state)
    assert end_event.fields["wav"] is None


def test_parse_dsdfme_no_sync_without_active_call_is_none():
    state = DsdState()
    assert parse_dsdfme_line("No Sync", "dmr", state) is None


def test_build_backend_dmr_argv(tmp_path):
    spec = DECODERS["dmr"]
    backend = build_backend(spec, {"dsd-fme": "/Users/x/.local/bin/dsd-fme"}, calls_dir=tmp_path)
    assert backend._argv[:-1] == [
        "/Users/x/.local/bin/dsd-fme",
        "-fs",
        "-i",
        "-",
        "-o",
        "null",
        "-7",
        str(tmp_path),
        "-P",
        "-J",
    ]
    assert pathlib.Path(backend._argv[-1]).parent == tmp_path
    assert pathlib.Path(backend._argv[-1]).name.startswith("dmr-")
    assert backend._event_log_path == pathlib.Path(backend._argv[-1])
    assert tmp_path.is_dir()


def test_build_backend_p25_argv(tmp_path):
    spec = DECODERS["p25"]
    backend = build_backend(spec, {"dsd-fme": "/x/dsd-fme"}, calls_dir=tmp_path)
    assert "-f1" in backend._argv
    assert pathlib.Path(backend._argv[-1]).name.startswith("p25-")


@pytest.mark.parametrize(("decoder_id", "flag"), [("nxdn", "-fi"), ("nxdn96", "-fn")])
def test_build_backend_nxdn_argv(tmp_path, decoder_id, flag):
    spec = DECODERS[decoder_id]
    backend = build_backend(spec, {"dsd-fme": "/x/dsd-fme"}, calls_dir=tmp_path)
    assert flag in backend._argv
    assert "-fa" not in backend._argv
    assert pathlib.Path(backend._argv[-1]).name.startswith(f"{decoder_id}-")


@pytest.mark.parametrize(
    ("decoder_id", "flag"),
    [("dstar", "-fd"), ("ysf", "-fy"), ("dpmr", "-fm"), ("m17", "-fz")],
)
def test_build_backend_dsdfme_extra_mode_argv(tmp_path, decoder_id, flag):
    backend = build_backend(DECODERS[decoder_id], {"dsd-fme": "/x/dsd-fme"}, calls_dir=tmp_path)
    assert backend._argv[1] == flag
    assert backend._argv[2:-1] == [
        "-i",
        "-",
        "-o",
        "null",
        "-7",
        str(tmp_path),
        "-P",
        "-J",
    ]
    assert pathlib.Path(backend._argv[-1]).name.startswith(f"{decoder_id}-")


def test_dsdfme_backends_use_separate_event_logs_for_same_protocol(tmp_path):
    first = build_backend(DECODERS["nxdn96"], {"dsd-fme": "/x/dsd-fme"}, calls_dir=tmp_path)
    second = build_backend(DECODERS["nxdn96"], {"dsd-fme": "/x/dsd-fme"}, calls_dir=tmp_path)
    assert first._event_log_path != second._event_log_path


def test_build_backend_dsdfme_raises_for_missing_binary():
    spec = DECODERS["dmr"]
    with pytest.raises(DecoderError):
        build_backend(spec, {"dsd-fme": None})


# -- PythonBackend (in-process decoders) -----------------------------


class _TrivialPyDecoder:
    """A minimal PyDecoder: input_rate 1000 Hz, echoes one event per batch
    and one on flush()."""

    input_rate = 1000

    def __init__(self):
        self.batches: list[int] = []

    def feed(self, samples) -> list[DecodeEvent]:
        self.batches.append(samples.size)
        return [
            DecodeEvent(
                ts=time.time(), decoder="trivial", kind="text", text=f"n={samples.size}", fields={}
            )
        ]

    def flush(self) -> list[DecodeEvent]:
        return [DecodeEvent(ts=time.time(), decoder="trivial", kind="text", text="done", fields={})]


class _CrashingPyDecoder:
    input_rate = 1000

    def feed(self, samples) -> list[DecodeEvent]:
        raise ValueError("boom")

    def flush(self) -> list[DecodeEvent]:
        return []


@pytest.mark.asyncio
async def test_python_backend_start_feed_stop_round_trip():
    decoder = _TrivialPyDecoder()
    backend = PythonBackend(decoder, "trivial")
    await backend.start()
    events_iter = backend.events()
    try:
        # 100ms @ 1000 Hz mono s16 = 100 samples = 200 bytes -> exactly one batch.
        backend.feed(b"\x00\x00" * 100)
        event = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
        assert event.text == "n=100"
        assert decoder.batches == [100]

        await backend.stop()
        final = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
        assert final.text == "done"
        assert backend.error is None
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_python_backend_surfaces_decoder_exception_as_error():
    backend = PythonBackend(_CrashingPyDecoder(), "crashing")
    await backend.start()
    backend.feed(b"\x00\x00" * 100)
    for _ in range(50):
        if backend.error is not None:
            break
        await asyncio.sleep(0.02)
    assert backend.error == "boom"
    await backend.stop()


def test_build_backend_python_wefax_creates_fax_dir(tmp_path):
    spec = DECODERS["wefax"]
    fax_dir = tmp_path / "fax"
    backend = build_backend(spec, {}, fax_dir=fax_dir)
    assert isinstance(backend, PythonBackend)
    assert backend.decoder_id == "wefax"
    assert fax_dir.is_dir()


def test_build_backend_python_navtex_needs_no_fax_dir():
    spec = DECODERS["navtex"]
    backend = build_backend(spec, {})
    assert isinstance(backend, PythonBackend)
    assert backend.decoder_id == "navtex"


def test_build_backend_wwv_dcf77_msf_construct_cleanly():
    # wwv/dcf77/msf (round 2) are real PyDecoders now -- build_backend
    # instantiates them with no binaries needed, same as wefax/navtex.
    for decoder_id in ("wwv", "dcf77", "msf"):
        backend = build_backend(DECODERS[decoder_id], {})
        assert backend.decoder_id == decoder_id


# -- integrity from the capability-matrix policy -------------------------


def test_policy_integrity_maps_each_policy():
    from msdr.decoder.backends import policy_integrity

    assert policy_integrity("aprs", "direwolf") == {
        "integrity": "passed",
        "integrity_note": "AX.25 FCS verified by direwolf",
    }
    assert policy_integrity("pocsag", "multimon-ng")["integrity"] == "unavailable"
    assert policy_integrity("wwv", "msdr")["integrity"] == "passed"
    # parity/fec need the decoder's own per-message result: never "passed".
    assert policy_integrity("dcf77", "msdr")["integrity"] == "unknown"
    assert policy_integrity("navtex", "msdr")["integrity"] == "unknown"
    assert policy_integrity("no-such-decoder", "x")["integrity"] == "unknown"


def test_parse_multimon_json_integrity_unavailable():
    event = parse_multimon_json(
        '{"demod_name":"POCSAG1200","address":1,"function":0,"alpha":"hi"}', "pocsag"
    )
    assert event.integrity == "unavailable"
    assert "multimon-ng" in event.integrity_note


def test_parse_multimon_plain_aprs_integrity_passed():
    event = parse_multimon_plain("APRS: N0CALL>APRS:>hello", "aprs")
    assert event.integrity == "passed"
    assert event.integrity_note == "AX.25 FCS verified by multimon-ng"


def test_parse_multimon_plain_cw_integrity_unavailable():
    event = parse_multimon_plain("CW: CQ CQ DE W1AW", "cw")
    assert event.integrity == "unavailable"


def test_parse_direwolf_line_integrity_passed():
    event = parse_direwolf_line("[0.3] W1AW-1>APRS,WIDE1-1:>test", "aprs")
    assert event.integrity == "passed"
    assert event.integrity_note == "AX.25 FCS verified by direwolf"


def test_parse_dsdfme_integrity_unavailable_and_sync_state():
    from msdr.decoder.backends import DsdState, parse_dsdfme_line

    state = DsdState()
    assert state.synchronized is None
    assert parse_dsdfme_line("Sync: +DMR  [slot1]  Color Code=01", "dmr", state) is None
    assert state.synchronized is True
    event = parse_dsdfme_line("TGT=123 SRC=456", "dmr", state)
    assert event.integrity == "unavailable"
    end = parse_dsdfme_line("No Sync", "dmr", state)
    assert end.integrity == "unavailable"
    assert state.synchronized is False


def test_line_coalescer_rtty_integrity_unavailable():
    from msdr.decoder.backends import LineCoalescer

    event = LineCoalescer("rtty").feed_line("RYRYRY")
    assert event.integrity == "unavailable"


def test_navtex_integrity_passed_when_every_character_resolved():
    from msdr.decoder.pydecoders.navtex import encode_message, words_to_bits
    from tests.decoder.test_pydecoders import _MESSAGE, _decode, _synth_navtex_audio

    (event,) = _decode(_synth_navtex_audio(words_to_bits(encode_message(_MESSAGE))))
    assert event.fields["errors"] == 0
    assert event.integrity == "passed"


def test_navtex_integrity_unknown_with_unresolved_characters():
    from msdr.decoder.pydecoders.navtex import _FEC_DELAY, encode_message, words_to_bits
    from tests.decoder.test_pydecoders import _MESSAGE, _decode, _synth_navtex_audio

    words = list(encode_message(_MESSAGE))
    beat = 20  # a body character: DX copy and its RX repeat both broken
    # One flipped bit breaks the 4-of-7 weight on both copies.
    words[beat * 2] ^= 0b0000001
    words[(beat + _FEC_DELAY) * 2 + 1] ^= 0b0000001
    (event,) = _decode(_synth_navtex_audio(words_to_bits(words)))
    assert event.fields["errors"] > 0
    assert event.integrity == "unknown"
    assert f"{event.fields['errors']} unresolved" in event.integrity_note


def _timecode_events(decoder_cls, labels, nominal, *, off, gap_and_next=False):
    from tests.decoder import test_timecode as tc

    if off:
        audio = tc._render_off_pulses(labels, nominal, tc._CARRIER_HZ, gap_and_next=gap_and_next)
    else:
        audio = tc._render_on_pulses(labels, nominal, 100.0)  # WWV's 100 Hz subcarrier
    return tc._feed_all(decoder_cls(clock=tc._clock_at_end(audio)), audio)


def test_wwv_integrity_passed_builtin_check():
    from datetime import UTC, datetime

    from msdr.decoder.pydecoders.timecode import WwvDecoder, encode_wwv_minute
    from tests.decoder import test_timecode as tc

    labels = encode_wwv_minute(datetime(2026, 9, 25, 20, 31, tzinfo=UTC))
    (event,) = _timecode_events(WwvDecoder, labels, tc._WWV_NOMINAL_S, off=False)
    assert event.integrity == "passed"


def test_dcf77_integrity_from_parity():
    from datetime import datetime

    from msdr.decoder.pydecoders.timecode import Dcf77Decoder, encode_dcf77_minute
    from tests.decoder import test_timecode as tc

    labels = encode_dcf77_minute(datetime(2026, 9, 25, 22, 31), dst=True)
    (good,) = _timecode_events(
        Dcf77Decoder, labels, tc._DCF77_NOMINAL_S, off=True, gap_and_next=True
    )
    assert good.integrity == "passed" and good.fields["parity_ok"] is True
    bad_labels = list(labels)
    bad_labels[28] = "1" if bad_labels[28] == "0" else "0"  # minute parity bit
    (bad,) = _timecode_events(
        Dcf77Decoder, bad_labels, tc._DCF77_NOMINAL_S, off=True, gap_and_next=True
    )
    assert bad.integrity == "failed"
    assert "minute" in bad.integrity_note and bad.fields["parity_ok"] is False


def test_msf_integrity_from_parity():
    from datetime import datetime

    from msdr.decoder.pydecoders.timecode import MsfDecoder, encode_msf_minute
    from tests.decoder import test_timecode as tc

    labels = encode_msf_minute(datetime(2026, 9, 25, 21, 31), dst=True)
    (good,) = _timecode_events(MsfDecoder, labels, tc._MSF_NOMINAL_S, off=True)
    assert good.integrity == "passed"
    bad_labels = list(labels)
    a, b = bad_labels[55][0], bad_labels[55][1]
    bad_labels[55] = a + ("1" if b == "0" else "0")  # hour parity bit
    (bad,) = _timecode_events(MsfDecoder, bad_labels, tc._MSF_NOMINAL_S, off=True)
    assert bad.integrity == "failed" and "hour" in bad.integrity_note


def test_wefax_events_integrity_not_applicable(tmp_path):
    from msdr.decoder.pydecoders.wefax import WefaxDecoder
    from tests.decoder.test_pydecoders import _synth_wefax_audio

    audio, _ = _synth_wefax_audio(5)
    decoder = WefaxDecoder(tmp_path)
    events = decoder.feed(audio) + decoder.flush()
    assert events and all(e.integrity == "not_applicable" for e in events)


@pytest.mark.asyncio
async def test_morse_stream_coalesces_letters_before_newline_or_exit():
    from msdr.decoder.backends import MorseBackend

    backend = MorseBackend([sys.executable, "-u", "-c",
        "import sys,time\nfor c in 'CQ DE K6ER CM87SU':\n"
        " sys.stdout.write(c); sys.stdout.flush(); time.sleep(.02)\n"
        "time.sleep(3)"], lambda line: None, decoder_id="cw")
    backend.phrase_idle_s = .2
    await backend.start()
    try:
        event = await asyncio.wait_for(anext(backend.events()), 2)
        assert event.text == "CQ DE K6ER CM87SU"
        assert event.decoder == "cw"
        assert backend._events.empty()
        assert backend._proc.returncode is None
    finally:
        await backend.stop()


def test_morse_input_gain_preserves_silence_and_saturates():
    import numpy as np

    from msdr.decoder.backends import MorseBackend

    backend = MorseBackend([], lambda line: None, decoder_id="cw")
    backend.feed(np.array([0, 100, -100, 10000, -10000], dtype="<i2").tobytes())
    values = np.frombuffer(backend._write_buf[0], dtype="<i2").tolist()
    assert values == [0, 400, -400, 32767, -32768]


@pytest.mark.asyncio
async def test_morse_continuous_text_has_bounded_display_delay():
    from msdr.decoder.backends import MorseBackend

    backend = MorseBackend([sys.executable, "-u", "-c",
        "import sys,time\nfor _ in range(100):\n"
        " sys.stdout.write('E'); sys.stdout.flush(); time.sleep(.03)"],
        lambda _: None, decoder_id="cw")
    backend.phrase_idle_s = .5
    backend.phrase_max_s = .15
    await backend.start()
    try:
        event = await asyncio.wait_for(anext(backend.events()), 1)
        assert len(event.text) > 1
        assert set(event.text) == {"E"}
        assert backend._proc.returncode is None
    finally:
        await backend.stop()


def test_cw_channel_does_not_raise_noise_during_key_up_gaps():
    import numpy as np

    from msdr.decoder.channel import DecodeChannel
    from msdr.decoder.registry import DECODERS

    class Sink:
        def __init__(self):
            self.levels = []

        def feed(self, raw):
            self.levels.append(float(np.abs(np.frombuffer(raw, dtype="<i2")).mean()))

    sink = Sink()
    channel = DecodeChannel(DECODERS["cw"], sink, 22050, 1)
    tone = (1000 * np.sin(2 * np.pi * 800 * np.arange(22050) / 22050)).astype("<i2")
    gap = np.tile(np.array([20, -20], dtype="<i2"), 11025)
    channel.feed(tone.tobytes())
    for _ in range(8):
        channel.feed(gap.tobytes())
    # A long key-up pause must not be AGC-boosted into apparent keyed energy.
    assert max(sink.levels[1:]) <= 20
    assert sink.levels[-1] == sink.levels[1]


@pytest.mark.asyncio
async def test_python_backend_stops_queueing_after_worker_crash_and_stop_is_prompt():
    backend = PythonBackend(_CrashingPyDecoder(), "crashing")
    await backend.start()
    backend.feed(b"\x00\x00" * 101)  # odd-looking sizes must not matter either
    for _ in range(50):
        if backend.error is not None:
            break
        await asyncio.sleep(0.02)
    assert backend.error == "boom"
    for _ in range(20):
        backend.feed(b"\x00\x00" * 100)
    assert backend._batch_queue.qsize() == 0  # dead worker: nothing piles up
    await asyncio.wait_for(backend.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_python_backend_odd_byte_batch_does_not_kill_worker():
    decoder = _TrivialPyDecoder()
    backend = PythonBackend(decoder, "trivial")
    await backend.start()
    try:
        backend.feed(b"\x00" * 201)  # one stray byte
        event = await asyncio.wait_for(backend.events().__anext__(), timeout=2.0)
        assert event.text == "n=100"
        assert backend.error is None
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_process_backend_failed_start_removes_dsdfme_event_log(tmp_path):
    from msdr.decoder.backends import _dsdfme_backend

    spec = DECODERS["dmr"]
    backend = _dsdfme_backend(spec, {"dsd-fme": str(tmp_path / "no-such-binary")}, tmp_path)
    assert list(tmp_path.glob("*-events.log"))
    with pytest.raises(OSError):
        await backend.start()
    assert not list(tmp_path.glob("*-events.log"))


def test_loop_event_queue_put_after_loop_closed_does_not_raise():
    import threading

    loop = asyncio.new_event_loop()
    queue = loop.run_until_complete(_make_loop_queue())
    loop.close()
    errors = []

    def put():
        try:
            queue.put_nowait(1)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=put)
    thread.start()
    thread.join()
    assert errors == []


async def _make_loop_queue():
    from msdr.decoder.backends import LoopEventQueue

    return LoopEventQueue()
