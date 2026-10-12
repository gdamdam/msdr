"""GET /api/signal-detail (U3, msdr/api/**, Stream A): aggregate memory/find/
survey-label/evidence/messages for one frequency across every "select a
signal" surface and
msdr/api/signal_detail.py."""

import pytest

from msdr.library.models import Signal
from tests.api.test_app import FakeSurvey, running_app


class _Event:
    """Duck-types msdr.decoder.backends.DecodeEvent for DecodeEventRepo.add()
    (mirrors tests/api/test_decoder.py's own _Event)."""

    def __init__(
        self,
        *,
        ts,
        decoder,
        text,
        frequency_hz,
        kind="text",
        fields=None,
        integrity="unknown",
        integrity_note="",
    ):
        self.ts = ts
        self.decoder = decoder
        self.text = text
        self.kind = kind
        self.fields = fields or {}
        self.frequency_hz = frequency_hz
        self.signal_id = None
        # DecodeEvent's integrity/integrity_note (default "unknown", same
        # as DecodeEventRepo.add's own getattr default) -- see
        # test_decoded_state_follows_event_integrity below.
        self.integrity = integrity
        self.integrity_note = integrity_note


# --- 404: only when neither frequency nor a resolvable id is given ----------


async def test_404_with_no_frequency_and_no_id():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/signal-detail")
        assert resp.status_code == 404


async def test_404_when_only_an_unresolvable_signal_id_is_given():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/signal-detail", params={"signal_id": "missing"})
        assert resp.status_code == 404


async def test_404_when_only_an_unresolvable_find_id_is_given():
    async with running_app() as (app, client, _adapter):
        resp = await client.get("/api/signal-detail", params={"find_id": "missing"})
        assert resp.status_code == 404


async def test_unresolvable_ids_are_null_not_404_when_frequency_is_given():
    async with running_app() as (app, client, _adapter):
        resp = await client.get(
            "/api/signal-detail",
            params={"frequency_hz": 100_000_000, "signal_id": "missing", "find_id": "missing"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory"] is None
        assert body["find"] is None
        assert body["survey_label"] is None
        # no memory/find/survey -> no decoded or memory evidence, whatever
        # bandplan/catalog matches exist at 100 MHz are unaffected by the
        # unresolved ids.
        assert {e["kind"] for e in body["evidence"]} <= {
            "scheduled", "bandplan", "allocation", "analyzer",
        }


# --- memory resolution (explicit id, or nearest by frequency) ---------------


async def test_frequency_only_resolves_nearby_memory():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        signal = Signal(name="Test Repeater", frequency_hz=146_520_000, notes="hi")
        service.signals.add(signal)

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 146_520_000})
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory"]["id"] == signal.id
        assert body["frequency_hz"] == 146_520_000


async def test_frequency_derived_from_signal_id_when_omitted():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        signal = Signal(name="Test Repeater", frequency_hz=146_520_000)
        service.signals.add(signal)

        resp = await client.get("/api/signal-detail", params={"signal_id": signal.id})
        assert resp.status_code == 200
        assert resp.json()["frequency_hz"] == 146_520_000


# --- evidence ordering: decoded -> memory -> scheduled -> bandplan -> analyzer


async def test_decoded_evidence_is_first_and_newest_message_wins():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        signal = Signal(name="Pager", frequency_hz=152_840_000, notes="ambulance dispatch")
        service.signals.add(signal)
        service.decode_events.add(
            _Event(ts=1.0, decoder="pocsag", text="alpha", frequency_hz=152_840_000)
        )
        service.decode_events.add(
            _Event(ts=2.0, decoder="pocsag", text="beta", frequency_hz=152_840_000)
        )

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        body = resp.json()
        kinds = [e["kind"] for e in body["evidence"]]
        assert kinds[0] == "decoded"
        assert kinds[1] == "memory"
        assert body["evidence"][0]["detail"] == "beta"  # newest ts wins
        assert [m["text"] for m in body["messages"]] == ["beta", "alpha"]


async def test_suggested_decoders_come_from_recent_messages_without_memory():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.decode_events.add(
            _Event(ts=1.0, decoder="pocsag", text="page", frequency_hz=152_840_000)
        )
        resp = await client.get("/api/signal-detail", params={"frequency_hz": 152_840_000})
        assert resp.json()["suggested_decoders"] == ["pocsag"]


@pytest.mark.parametrize("integrity", [None, "failed", "unknown", "passed"])
async def test_decoded_evidence_falls_back_to_survey_label_confirmed(integrity):
    fake = FakeSurvey()
    fake.active = True

    def status_with_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000  # tolerance ~3906 Hz
        base["labels"] = [
            {
                "frequency_hz": 146_521_000,
                "confirmed": {
                    "decoder": "aprs",
                    "sample": "N0CALL>APRS",
                    "at": 5.0,
                    **({"integrity": integrity} if integrity is not None else {}),
                },
                "family": None,
                "modulation": None,
            }
        ]
        return base

    fake.status = status_with_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        service._identify_for_survey = lambda *_a, **_k: []
        resp = await client.get("/api/signal-detail", params={"frequency_hz": 146_520_000})
        assert resp.status_code == 200
        body = resp.json()
        decoded = next(e for e in body["evidence"] if e["kind"] == "decoded")
        assert decoded["source"] == "aprs"
        assert decoded["detail"] == "N0CALL>APRS"
        assert decoded["integrity"] == (integrity or "unknown")
        assert decoded["evidence_type"] == (
            "validated_decode" if integrity == "passed" else "decoder_output"
        )
        assert body["messages"] == []


async def test_rds_evidence_first_with_message_second():
    fake = FakeSurvey()
    fake.active = True

    def status_with_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000  # tolerance ~3906 Hz
        base["labels"] = [
            {
                "frequency_hz": 97_100_000,
                "confirmed": {"decoder": "rds", "sample": "KROQ · Top Hits", "at": 5.0},
                "family": {"name": "FM broadcast · KROQ"},
                "modulation": None,
                "rds": {
                    "pi": "0x1234",
                    "ps": "KROQ",
                    "pty": 10,
                    "pty_name": "Rock",
                    "rt": "Top Hits",
                    "tp": True,
                    "groups": 42,
                    "block_error_rate": 0.1,
                },
                "last_seen": 9.0,
            }
        ]
        return base

    fake.status = status_with_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        service._identify_for_survey = lambda *_a, **_k: []
        service.decode_events.add(
            _Event(ts=1.0, decoder="pocsag", text="unrelated", frequency_hz=97_100_000)
        )

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 97_100_000})
        assert resp.status_code == 200
        body = resp.json()
        decoded_items = [e for e in body["evidence"] if e["kind"] == "decoded"]
        # PI (validated) and PS (estimated) are split, never merged into a
        # single confidence number.
        assert decoded_items[0]["title"] == "PI 0x1234"
        assert decoded_items[0]["evidence_type"] == "validated_decode"
        assert decoded_items[0]["state"] == "validated"
        assert "confidence" not in decoded_items[0]
        assert decoded_items[1]["title"] == "RDS: KROQ"
        assert decoded_items[1]["detail"] == "Rock · Top Hits"
        assert decoded_items[1]["evidence_type"] == "decoder_output"
        assert decoded_items[1]["state"] == "estimated"
        assert decoded_items[1]["source"] == "rds"
        assert decoded_items[1]["at"] == 9.0
        assert decoded_items[2]["source"] == "pocsag"


async def test_memory_evidence_uses_name_and_notes():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        signal = Signal(name="Weather Station", frequency_hz=162_550_000, notes="NOAA WX")
        service.signals.add(signal)

        resp = await client.get(
            "/api/signal-detail", params={"frequency_hz": 162_550_000, "signal_id": signal.id}
        )
        body = resp.json()
        memory_ev = next(e for e in body["evidence"] if e["kind"] == "memory")
        assert memory_ev["title"] == "Weather Station"
        assert memory_ev["detail"] == "NOAA WX"
        # Not yet identified -- context, not operator certainty.
        assert memory_ev["evidence_type"] == "operator"
        assert memory_ev["state"] == "context"
        assert "confidence" not in memory_ev


async def test_memory_evidence_is_operator_state_once_identified():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        signal = Signal(
            name="Weather Station",
            frequency_hz=162_550_000,
            notes="NOAA WX",
            status="identified",
        )
        service.signals.add(signal)

        resp = await client.get(
            "/api/signal-detail", params={"frequency_hz": 162_550_000, "signal_id": signal.id}
        )
        body = resp.json()
        memory_ev = next(e for e in body["evidence"] if e["kind"] == "memory")
        assert memory_ev["state"] == "operator"


async def test_scheduled_only_includes_on_air_and_bandplan_is_separate(monkeypatch):
    async with running_app() as (app, client, _adapter):
        service = app.state.service

        def fake_identify(frequency_hz, width_hz):
            return [
                {
                    "name": "on-air-station",
                    "category": "bc",
                    "confidence": 0.85,
                    "reason": "9.4 MHz: on-air-station, 0600-2200 UTC",
                    "provider": "catalog",
                },
                {
                    "name": "off-air-station",
                    "category": "bc",
                    "confidence": 0.45,
                    "reason": "9.4 MHz: off-air-station, 0000-0100 UTC",
                    "provider": "catalog",
                },
                {
                    "name": "shortwave broadcast",
                    "category": "bc",
                    "confidence": 0.5,
                    "reason": "9.4 MHz: shortwave broadcast band",
                    "provider": "bandplan",
                },
            ]

        monkeypatch.setattr(service, "_identify_for_survey", fake_identify)

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 9_400_000})
        assert resp.status_code == 200
        body = resp.json()
        kinds = [e["kind"] for e in body["evidence"]]
        assert kinds == ["scheduled", "bandplan", "allocation"]
        scheduled = body["evidence"][0]
        assert scheduled["title"] == "on-air-station"
        assert scheduled["evidence_type"] == "schedule"
        assert scheduled["state"] == "context"
        assert scheduled["score"] is None
        assert "confidence" not in scheduled
        bandplan = body["evidence"][1]
        assert bandplan["title"] == "shortwave broadcast"
        assert bandplan["evidence_type"] == "allocation"
        assert bandplan["state"] == "context"
        assert bandplan["score"] is None
        assert "confidence" not in bandplan


async def test_analyzer_evidence_with_confidence_from_survey_label(monkeypatch):
    fake = FakeSurvey()
    fake.active = True

    def status_with_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000  # tolerance ~3906 Hz
        base["labels"] = [
            {
                "frequency_hz": 146_521_000,  # 1 kHz away -- within tolerance
                "activity": {"kind": "busy", "duty": 0.5},
                "bw_class": "narrow",
                "modulation": {"kind": "fm", "confidence": 0.6},
                "symbol_rate_hz": 1200.0,
                "family": {
                    "name": "APRS",
                    "suggested_decoders": ["multimon-ng"],
                    "confidence": 0.72,
                },
                "voice": None,
                "confirmed": None,
                "analyzed_at": 123.0,
            }
        ]
        return base

    fake.status = status_with_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        monkeypatch.setattr(service, "_identify_for_survey", lambda *_a, **_k: [])

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 146_520_000})
        assert resp.status_code == 200
        body = resp.json()
        assert body["survey_label"]["frequency_hz"] == 146_521_000
        analyzer_ev = next(e for e in body["evidence"] if e["kind"] == "analyzer")
        assert analyzer_ev["title"] == "APRS"
        assert analyzer_ev["evidence_type"] == "iq_estimate"
        assert analyzer_ev["state"] == "estimated"
        assert analyzer_ev["score"] == 0.72
        assert "confidence" not in analyzer_ev
        assert analyzer_ev["source"] == "analyzer"
        assert analyzer_ev["source_group"] == "dsp"
        assert analyzer_ev["at"] == 123.0
        assert body["suggested_decoders"] == ["multimon-ng"]


# --- tolerance edges ---------------------------------------------------------


async def test_decode_message_tolerance_boundary():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.decode_events.add(
            _Event(ts=1.0, decoder="pocsag", text="at boundary", frequency_hz=152_846_000)
        )  # exactly 6000 Hz away -- included
        service.decode_events.add(
            _Event(ts=2.0, decoder="pocsag", text="just outside", frequency_hz=152_846_001)
        )  # 6001 Hz away -- excluded

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        texts = [m["text"] for m in resp.json()["messages"]]
        assert "at boundary" in texts
        assert "just outside" not in texts


async def test_survey_label_outside_tolerance_is_null():
    fake = FakeSurvey()
    fake.active = True

    def status_with_far_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000  # tolerance ~3906 Hz
        base["labels"] = [{"frequency_hz": 146_600_000, "activity": {"kind": "busy", "duty": 0.5}}]
        return base

    fake.status = status_with_far_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        service._identify_for_survey = lambda *_a, **_k: []
        resp = await client.get("/api/signal-detail", params={"frequency_hz": 146_520_000})
        assert resp.status_code == 200
        assert resp.json()["survey_label"] is None


# --- observations -------------------------------------------------------


def test_last_classification_recovers_superseded_voice_but_ignores_generic_and_replay():
    from msdr.api.signal_detail import _last_classification

    base = {"producer": "survey.analyzer", "subject": "modulation",
            "observed_start": "2026-09-28T19:00:00+00:00"}
    observations = [{"claims": [
        {**base, "value": "speech-like audio", "historical": True},
        {**base, "value": "fm_fsk", "observed_start": "2026-09-28T19:01:00+00:00"},
        {**base, "value": "data-like", "analysis_run_id": "replay",
         "observed_start": "2026-09-28T19:02:00+00:00"},
    ]}]
    result = _last_classification(observations, None)
    assert result["name"] == "speech-like audio"
    assert _last_classification([], None) is None


async def test_no_evidence_item_has_a_confidence_key(monkeypatch):
    """every evidence item lost `confidence` in favour of state/
    evidence_type/score."""
    async with running_app() as (app, client, _adapter):
        service = app.state.service

        def fake_identify(frequency_hz, width_hz):
            return [
                {
                    "name": "shortwave broadcast",
                    "category": "bc",
                    "confidence": 0.5,
                    "reason": "9.4 MHz: shortwave broadcast band",
                    "provider": "bandplan",
                }
            ]

        monkeypatch.setattr(service, "_identify_for_survey", fake_identify)
        signal = Signal(name="Test", frequency_hz=9_400_000, notes="n")
        service.signals.add(signal)

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 9_400_000})
        assert resp.status_code == 200
        body = resp.json()
        assert body["evidence"], "expected at least one evidence item"
        for item in body["evidence"]:
            assert "confidence" not in item
            assert "state" in item
            assert "evidence_type" in item
            assert "score" in item


async def test_signal_detail_includes_nearby_observations():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        service.obs_recorder.on_scanner_hit(
            {"ts": 100.0, "duration_s": 1.0, "frequency_hz": 152_840_000, "peak_snr_db": 12.0},
            "find-1",
        )

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["observations"]) == 1
        assert body["observations"][0]["reported_hz"] == 152_840_000


# --- decoded state follows the event's own integrity, candidate support -


async def test_decoded_state_is_validated_when_event_integrity_passed():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.decode_events.add(
            _Event(
                ts=1.0,
                decoder="aprs",
                text="N0CALL>APRS:hi",
                frequency_hz=144_390_000,
                integrity="passed",
                integrity_note="AX.25 FCS verified by direwolf",
            )
        )
        resp = await client.get("/api/signal-detail", params={"frequency_hz": 144_390_000})
        assert resp.status_code == 200
        decoded = next(e for e in resp.json()["evidence"] if e["kind"] == "decoded")
        assert decoded["evidence_type"] == "validated_decode"
        assert decoded["state"] == "validated"
        assert decoded["integrity"] == "passed"
        assert decoded["integrity_note"] == "AX.25 FCS verified by direwolf"


async def test_decoded_state_is_estimated_when_event_integrity_is_not_passed():
    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.decode_events.add(
            _Event(
                ts=1.0,
                decoder="pocsag",
                text="page",
                frequency_hz=152_840_000,
                integrity="unavailable",
                integrity_note="multimon-ng exposes no validity flag",
            )
        )
        resp = await client.get("/api/signal-detail", params={"frequency_hz": 152_840_000})
        assert resp.status_code == 200
        decoded = next(e for e in resp.json()["evidence"] if e["kind"] == "decoded")
        assert decoded["evidence_type"] == "decoder_output"
        assert decoded["state"] == "estimated"
        assert decoded["integrity"] == "unavailable"


async def test_candidate_evidence_carries_support_alternatives_next_action(monkeypatch):
    """the candidate (analyzer family) evidence item carries `support`
    (raw/snapped rate, tolerance, modulation), `alternatives` and
    `next_action` straight from the analyzer's own fields."""
    fake = FakeSurvey()
    fake.active = True

    def status_with_label():
        base = FakeSurvey.status(fake)
        base["span"]["sample_rate_hz"] = 2_000_000
        base["labels"] = [
            {
                "frequency_hz": 146_520_000,
                "activity": {"kind": "busy", "duty": 0.5},
                "bw_class": "narrow",
                "modulation": {"kind": "fm_fsk", "confidence": 0.8},
                "symbol_rate_hz": 9600.0,
                "symbol_rate_raw_hz": 9612.0,
                "symbol_rate_snapped_hz": 9600.0,
                "symbol_rate_tolerance": 0.03,
                "family": {
                    "name": "GMSK/FSK 9600 (AIS or packet)",
                    "suggested_decoders": ["ais", "packet9600"],
                    "alternatives": ["AX.25 9600 (G3RUH)", "P25 C4FM (4800 sym/s x2)"],
                    "next_action": "decode ais or packet9600; AIS identity requires a "
                    "CRC-valid frame with an MMSI",
                    "confidence": 0.65,
                },
                "voice": None,
                "confirmed": None,
                "analyzed_at": 42.0,
            }
        ]
        return base

    fake.status = status_with_label
    async with running_app(survey=fake) as (app, client, _adapter):
        service = app.state.service
        monkeypatch.setattr(service, "_identify_for_survey", lambda *_a, **_k: [])

        resp = await client.get("/api/signal-detail", params={"frequency_hz": 146_520_000})
        assert resp.status_code == 200
        analyzer_ev = next(e for e in resp.json()["evidence"] if e["kind"] == "analyzer")
        assert analyzer_ev["alternatives"] == ["AX.25 9600 (G3RUH)", "P25 C4FM (4800 sym/s x2)"]
        assert analyzer_ev["next_action"].startswith("decode ais or packet9600")
        support = " ".join(analyzer_ev["support"])
        assert "9612" in support
        assert "9600" in support
        assert "3%" in support or "3 %" in support or "0.03" in support
        assert "fm_fsk" in support


async def test_stored_removed_rfml_claim_loads_without_promoting_evidence():
    """Databases written while the experimental RF ML feature existed still
    hold survey.rfml / survey.rfml.detector claims; they must stay readable."""
    from msdr.contracts.evidence import Claim

    async with running_app() as (app, client, _adapter):
        service = app.state.service
        service.obs_recorder.start_session("live:sdrconnect", "live", "127.0.0.1")
        service.obs_recorder.on_scanner_hit(
            {"ts": 100.0, "duration_s": 1.0, "frequency_hz": 160_000_000, "peak_snr_db": 12.0},
            "find-1",
        )
        obs = service.observations.list(frequency_hz=160_000_000, limit=1)[0]
        for producer, subject, value in (
            ("survey.rfml", "modulation", "FSK"),
            ("survey.rfml.detector", "presence", "ML signal proposal"),
        ):
            service.obs_recorder.repo.add_claim(Claim(
                observation_id=obs.id, subject=subject, value=value,
                evidence_type="iq_estimate", integrity="unknown", producer=producer,
            ))
        response = await client.get("/api/signal-detail", params={"frequency_hz": 160_000_000})
        assert response.status_code == 200
        body = response.json()
        producers = {c["producer"] for o in body["observations"] for c in o["claims"]}
        assert {"survey.rfml", "survey.rfml.detector"} <= producers
        assert all(e.get("source_group") != "ml" for e in body["evidence"])


async def test_off_center_aircraft_frequency_has_allocation_context():
    async with running_app() as (_app, client, _adapter):
        response = await client.get('/api/signal-detail', params={'frequency_hz': 1_089_974_844})
        assert response.status_code == 200
        allocation = next(e for e in response.json()['evidence'] if e['kind'] == 'bandplan')
        assert allocation['category'] == 'air'
        assert 'Aviation radionavigation' in allocation['title']
        assert allocation['state'] == 'context'
