"""ObservationRecorder: turns survey labels, decode events and scanner hits
into Observations + append-only Claims.

Pure logic over an `ObservationRepo` with an injectable clock. The survey
calls `sync_labels` at label *boundaries* only (a label appears, a dimension
result lands, a label retires) -- never per spectrum frame -- and the
recorder diffs against its own cache so an unchanged label costs zero
writes. A claim is appended when a dimension first appears or its value
changes; the previous claim is superseded, never overwritten.

Evidence honesty rules live in the claim mapping below: allocation and
schedule are context, an IQ estimate names candidates, a keyed carrier is a
*presence* claim (never "CW"), and a decoder's output is only
`validated_decode` when its backend exposes a real integrity check.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from msdr.contracts.evidence import Claim, Measurement
from msdr.observations.models import Observation, RecordingReference, Session
from msdr.observations.repo import ObservationRepo
from msdr.survey.families import KEYED_CARRIER

DECODE_TOLERANCE_HZ = 6000


def _same_channel(a: int | None, b: int | None) -> bool:
    """Same analysis-run partition's frequency: both unset, or within decode
    tolerance -- never a bare `==`, since a demod can report a few Hz of jitter
    for the same channel."""
    if a is None or b is None:
        return a is b
    return abs(a - b) <= DECODE_TOLERANCE_HZ


# Integrity policies that are a real per-message check (capability matrix).
_VALIDATING_POLICIES = frozenset({"fcs", "crc", "parity", "fec", "builtin_check"})
_NONE_EXPOSED_LIMITATION = "backend exposes no per-message integrity flag"
_SCHEDULE_LIMITATION = "schedule predicts a transmitter; not reception"
_PS_LIMITATION = "PS text can change"
_KEYED_ACTIVITY = frozenset({"keyed", "ook"})
_SPEECH_VOICE = frozenset({"speech", "voice"})  # analyzer says "voice", contract "speech"
_QUIET_VOICE = frozenset({"quiet", "silence"})  # analyzer says "silence", contract "quiet"
# Family names that are really a keyed-carrier guess: they become a presence
# claim's next action, never a protocol claim (msdr.survey.families'
# family_for names an *unconfirmed* keyed carrier KEYED_CARRIER, "CW" only
# once features.keying_timing's dot/dash statistics actually fit).
_KEYED_FAMILIES = frozenset({"CW", KEYED_CARRIER})
# Family names that are really a voice/speech read, not a protocol
# identification -- these become a `modulation` claim (wording,
# msdr.survey.analyzer's voice branch), same honesty rule as CW/keyed above:
# a demodulation guess is never presented as a decoded protocol.
_VOICE_FAMILY_MODULATION = {
    "NFM voice (analog)": "NFM voice",
    "speech-like NFM audio (tentative)": "speech-like NFM audio (tentative)",
    "speech-like audio (demodulation untested)": "speech-like audio",
}


def _integrity_policy(decoder_id: str) -> str | None:
    # msdr.decoder is optional at import (numpy/binaries, see its __init__);
    # without it we simply don't know the policy -- which is not "passed".
    try:
        from msdr.decoder.capability_matrix import MATRIX, RDS_ENTRY
    except ImportError:
        return None
    if decoder_id == "rds":
        return RDS_ENTRY.integrity_policy
    entry = MATRIX.get(decoder_id)
    return entry.integrity_policy if entry else None


def decoder_grade(decoder_id: str) -> tuple[str, str, list[str]]:
    """(evidence_type, integrity, limitations) for a decoder's output, per
    the capability matrix's integrity policy. Shared by the live recorder
    and the legacy import so both apply the same rule."""
    policy = _integrity_policy(decoder_id)
    if policy in _VALIDATING_POLICIES:
        return "validated_decode", "passed", []
    if policy == "none_exposed":
        return "decoder_output", "unavailable", [_NONE_EXPOSED_LIMITATION]
    # repeat_fields, or a decoder the matrix doesn't list: a check may exist
    # but nothing here has confirmed it ran.
    return "decoder_output", "unknown", [f"integrity policy: {policy or 'not recorded'}"]


def decoder_spec(decoder_id: str) -> dict:
    """Claim kwargs (subject/value/type/integrity/producer/limitations) for
    a decoder's output."""
    evidence_type, integrity, limitations = decoder_grade(decoder_id)
    return {
        "subject": "protocol",
        "value": decoder_id,
        "evidence_type": evidence_type,
        "integrity": integrity,
        "producer": f"decoder.{decoder_id}",
        "limitations": limitations,
    }


def _decode_claim_spec(decoder_id: str, event: dict) -> dict:
    """`decoder_spec()`, but the event's own integrity result -- when it has
    one -- overrides the capability matrix's policy guess. A decoder that
    *can* check (e.g. DCF77/MSF parity) is not the same as *this* event
    having passed that check; live and replay decode both go through here so
    a `failed` per-event integrity can never surface as `validated_decode`."""
    # A capability says the decoder *may* validate a frame; it does not prove
    # that this event was checked. Only an explicit per-event pass validates.
    spec = decoder_spec(decoder_id)
    integrity = event.get("integrity") or "unknown"
    etype = "validated_decode" if integrity == "passed" else "decoder_output"
    limitations = spec.get("limitations", [])
    if integrity == "unknown" and _NONE_EXPOSED_LIMITATION not in limitations:
        limitations = [*limitations, "event integrity was not recorded"]
    return {**spec, "integrity": integrity, "evidence_type": etype, "limitations": limitations}


def _decode_support(event_id: str, event: dict) -> list[str]:
    support = [f"decode event {event_id}"]
    if event.get("text"):
        support.append(f"sample: {event['text']}")
    return support


@dataclass
class _Tracked:
    obs: Observation
    # slot -> ((value, evidence_type, integrity), claim_id): the current claim
    # for each dimension. Evidence can strengthen while a decoder's label/value
    # stays the same (for example, first failed parity frame then a passed one).
    slots: dict[str, tuple[tuple[str, str, str], str]] = field(default_factory=dict)
    last_label: dict = field(default_factory=dict)


@dataclass
class _AnalysisPartition:
    """One (source_id, generation, channel) slice of an analysis run: its own
    Observation, so a run that visits several frequencies or survives a seek
    (a new generation) never merges unrelated channels into one row."""

    obs_id: str
    source_id: str | None
    generation: int | None
    freq: int | None
    ref: RecordingReference | None = None


class ObservationRecorder:
    def __init__(
        self,
        repo: ObservationRepo,
        *,
        now: Callable[[], float] = time.time,
        occupancy_window_s: float = 600.0,
    ) -> None:
        self.repo = repo
        self._now = now
        # The tracker's history window: what a label's duty cycle divides by
        # (Survey builds its tracker with windows_s[1], default 600 s).
        self._occupancy_window_s = occupancy_window_s
        self.session: Session | None = None
        self._labels: dict[str, _Tracked] = {}
        # Observations opened by decode events (no survey label) -> decoders
        # already claimed on them.
        self._decode_obs: dict[str, set[str]] = {}
        # Current append-only claim per survey-less observation/decoder. A
        # later event can supersede it when a real integrity check succeeds.
        self._decode_claims: dict[tuple[str, str], Claim] = {}
        # analysis_run_id -> its open _AnalysisPartition rows (review
        # decode, fixed: a run's events link to one Observation per
        # source_id/generation/frequency, not one Observation for the whole
        # run -- a run can visit several channels and survive seeks).
        self._analysis_obs: dict[str, list[_AnalysisPartition]] = {}

    # -- sessions ---------------------------------------------------------

    def start_session(
        self,
        source_id: str,
        mode: str,
        origin: str = "",
        center_hz: int | None = None,
        sample_rate_hz: int | None = None,
        receiver: dict | None = None,
    ) -> Session:
        if self.session is not None:
            self.end_session()
        self.session = self.repo.open_session(
            Session(
                source_id=source_id,
                mode=mode,
                origin=origin,
                started_at=self._now(),
                center_hz=center_hz,
                sample_rate_hz=sample_rate_hz,
                receiver=receiver,
            )
        )
        return self.session

    def end_session(self, *, sample_index: int | None = None) -> None:
        now = self._now()
        for label_id in list(self._labels):
            self._retire(label_id, now, sample_index)
        for obs_id in self._decode_obs:
            # end_ts already tracks the last event; only mark it closed.
            self.repo.close(obs_id, None, None)
        self._decode_obs.clear()
        self._decode_claims.clear()
        if self.session is not None:
            self.repo.close_session(self.session.id, now)
        self.session = None

    @property
    def _session_id(self) -> str | None:
        return self.session.id if self.session else None

    @property
    def _live(self) -> bool:
        # Without a session we assume the processing clock is capture time
        # (live); a replay's capture time is unknown and stays None.
        return self.session is None or self.session.mode == "live"

    # -- survey labels ----------------------------------------------------

    def sync_labels(self, labels: list[dict], *, sample_index: int | None = None) -> None:
        now = self._now()
        seen: set[str] = set()
        for label in labels:
            label_id = label.get("id")
            if label_id is None:
                continue
            seen.add(label_id)
            tracked = self._labels.get(label_id)
            if tracked is None:
                tracked = self._open_for_label(label, now, sample_index)
                self._labels[label_id] = tracked
            self._apply(tracked, label, now, sample_index)
            tracked.last_label = label
        for label_id in [lid for lid in self._labels if lid not in seen]:
            self._retire(label_id, now, sample_index)

    def _measurements(self, label: dict) -> list[Measurement]:
        out: list[Measurement] = []
        snr = label.get("snr_db")
        if snr is not None:
            out.append(
                Measurement(
                    name="snr",
                    value=float(snr),
                    unit="dB",
                    method="spectrum_display_snr",
                    reference="spectrum noise floor",
                )
            )
        activity = label.get("activity") or {}
        duty = activity.get("duty")
        if duty is not None:
            out.append(
                Measurement(
                    name="duty",
                    value=float(duty) * 100.0,
                    unit="%",
                    method="spectrum_occupancy",
                    denominator="occupancy window",
                    window_s=float(activity.get("window_s") or self._occupancy_window_s),
                )
            )
        return out

    def _open_for_label(self, label: dict, now: float, sample_index: int | None) -> _Tracked:
        width = label.get("width_hz")
        obs = Observation(
            session_id=self._session_id,
            start_ts=now if self._live else None,
            sample_start=sample_index,
            reported_hz=label.get("frequency_hz"),
            bandwidth_hz=int(round(width)) if width is not None else None,
            measurements=self._measurements(label),
            provenance="survey label",
            created_at=now,
            updated_at=now,
        )
        return _Tracked(obs=self.repo.add(obs))

    def _retire(self, label_id: str, now: float, sample_index: int | None) -> None:
        tracked = self._labels.pop(label_id)
        # One write: the last-known measurements ride along with the close,
        # so intermediate boundary calls never rewrite the row.
        obs = tracked.obs.model_copy(
            update={
                "measurements": self._measurements(tracked.last_label) or tracked.obs.measurements,
                "end_ts": now if self._live else None,
                "sample_end": sample_index if sample_index is not None else tracked.obs.sample_end,
                "closed": True,
            }
        )
        self.repo.update(obs)

    def _claim_fields(self, obs_id: str, now: float, sample_index: int | None) -> dict:
        at = datetime.fromtimestamp(now, UTC)
        return {
            "observation_id": obs_id,
            "source_id": self.session.source_id if self.session else None,
            "observed_start": at if self._live else None,
            "observed_end": at if self._live else None,
            "sample_start": sample_index,
            "sample_end": sample_index,
            "created_at": at,
        }

    def _apply(self, tracked: _Tracked, label: dict, now: float, sample_index: int | None) -> None:
        base = self._claim_fields(tracked.obs.id, now, sample_index)
        for slot, spec in self._claim_specs(label).items():
            current = tracked.slots.get(slot)
            signature = (spec["value"], spec["evidence_type"], spec["integrity"])
            if current is not None and current[0] == signature:
                continue  # unchanged dimension: no write
            claim = Claim(**base, **spec)
            if current is None:
                claim = self.repo.add_claim(claim)
            else:
                claim = self.repo.supersede(current[1], claim)
            tracked.slots[slot] = (signature, claim.claim_id)

        voice = label.get("voice") or {}
        if voice.get("kind") in _QUIET_VOICE and "quiet" not in tracked.obs.capture_quality:
            # Quiet is capture quality, not evidence about the signal.
            tracked.obs = tracked.obs.model_copy(
                update={"capture_quality": [*tracked.obs.capture_quality, "quiet"]}
            )
            self.repo.update(tracked.obs)

    def _claim_specs(self, label: dict) -> dict[str, dict]:
        """slot -> Claim kwargs (minus the observation/time fields). The
        slot's `value` is what the diff compares, so a jittering score alone
        never appends a claim."""
        specs: dict[str, dict] = {}

        bandplan = label.get("bandplan") or {}
        if bandplan.get("name"):
            specs["bandplan"] = {
                "subject": "service",
                "value": bandplan["name"],
                "evidence_type": "allocation",
                "integrity": "not_applicable",
                "producer": "survey.bandplan",
            }

        # docs/allocations.md: every service of the band, one claim, so the
        # value changes only when the allocation does.
        allocation_services = (label.get("allocation") or {}).get("services") or []
        allocation_value = "; ".join(
            dict.fromkeys(
                " ".join(p for p in (s.get("name"), s.get("qualifier")) if p)
                + (" (secondary)" if s.get("priority") == "secondary" else "")
                for s in allocation_services
                if s.get("name")
            )
        )
        if allocation_value:
            specs["allocation"] = {
                "subject": "service",
                "value": allocation_value,
                "evidence_type": "allocation",
                "integrity": "not_applicable",
                "producer": "survey.allocation",
            }

        for entry in label.get("catalog") or []:
            name = (entry or {}).get("name")
            if name:
                specs[f"catalog:{name}"] = {
                    "subject": "station",
                    "value": name,
                    "evidence_type": "schedule",
                    "integrity": "not_applicable",
                    "producer": "survey.catalog",
                    "limitations": [_SCHEDULE_LIMITATION],
                }

        modulation = label.get("modulation") or {}
        family = label.get("family") or {}
        activity = label.get("activity") or {}
        rds = label.get("rds") or {}
        mod_kind = modulation.get("kind")
        keyed = mod_kind == "ook" or activity.get("kind") in _KEYED_ACTIVITY
        family_name = family.get("name")
        suggested = family.get("suggested_decoders") or []

        if mod_kind and mod_kind != "ook":
            specs["modulation"] = {
                "subject": "modulation",
                "value": mod_kind,
                "evidence_type": "iq_estimate",
                "integrity": "unknown",
                "producer": "survey.analyzer",
                "score": modulation.get("confidence"),
                "alternatives": list(family.get("alternatives") or []),
            }

        if keyed:
            spec = {
                "subject": "presence",
                "value": "keyed carrier",
                "evidence_type": "iq_estimate",
                "integrity": "unknown",
                "producer": "survey.analyzer",
                "score": modulation.get("confidence") if mod_kind == "ook" else None,
            }
            if suggested and family_name in _KEYED_FAMILIES:
                spec["next_action"] = "decode " + ", ".join(suggested)
            specs["presence"] = spec

        # A keyed-carrier "family" (CW) is not a protocol identification, and
        # the RDS path's "FM broadcast · <name>" family is covered by the RDS
        # claims below -- neither becomes an iq_estimate protocol claim.
        rds_family = bool(rds) and str(family_name or "").startswith("FM broadcast")
        voice_modulation_value = _VOICE_FAMILY_MODULATION.get(family_name or "")
        if voice_modulation_value is not None:
            # A voice/speech read is a modulation claim, never a protocol
            # identification (wording: "NFM voice (analog)" / "speech-like
            # audio (demodulation untested)") -- same honesty rule as the
            # keyed-carrier presence claim above.
            specs["family"] = {
                "subject": "modulation",
                "value": voice_modulation_value,
                "evidence_type": "iq_estimate",
                "integrity": "unknown",
                "producer": "survey.analyzer",
                "score": family.get("confidence"),
                "support": _analyzer_support(label),
                "alternatives": list(family.get("alternatives") or []),
                "next_action": family.get("next_action") or "",
            }
        elif family_name and family_name not in _KEYED_FAMILIES and not rds_family:
            specs["family"] = {
                "subject": "protocol",
                "value": family_name,
                "evidence_type": "iq_estimate",
                "integrity": "unknown",
                "producer": "survey.analyzer",
                "score": family.get("confidence"),
                "support": _analyzer_support(label),
                "alternatives": list(family.get("alternatives") or []),
                "next_action": ("decode " + ", ".join(suggested)) if suggested else "",
            }

        voice = label.get("voice") or {}
        voice_kind = voice.get("kind")
        voice_value = (
            "speech-like audio"
            if voice_kind in _SPEECH_VOICE
            else "data-like"
            if voice_kind == "data"
            else None
        )
        if voice_value:
            specs["voice"] = {
                "subject": "modulation",
                "value": voice_value,
                "evidence_type": "iq_estimate",
                "integrity": "unknown",
                "producer": "survey.analyzer",
                "score": voice.get("score"),
            }

        confirmed = label.get("confirmed") or {}
        decoder_id = confirmed.get("decoder")
        if decoder_id and decoder_id != "rds":  # RDS is split into PI/PS below
            support = []
            supporting_sample = confirmed.get("integrity_sample") or confirmed.get("sample")
            if supporting_sample:
                support.append(f"sample: {supporting_sample}")
            if confirmed.get("events") is not None:
                support.append(f"{confirmed['events']} decode events")
            specs[f"confirmed:{decoder_id}"] = {
                **_decode_claim_spec(decoder_id, confirmed),
                "support": support,
            }

        pi = rds.get("pi")
        if pi and (rds.get("groups") or 0) > 0:
            pi_text = pi if str(pi).lower().startswith("0x") else f"0x{int(pi):04X}"
            support = [f"{rds['groups']} RDS groups"]
            if rds.get("block_error_rate") is not None:
                support.append(f"block error rate {rds['block_error_rate']}")
            specs["rds_pi"] = {
                "subject": "station",
                "value": f"PI {pi_text}",
                "evidence_type": "validated_decode",
                "integrity": "passed",
                "producer": "survey.rds",
                "support": support,
            }
        ps = (rds.get("ps") or "").strip()
        if ps:
            specs["rds_ps"] = {
                "subject": "station",
                "value": ps,
                "evidence_type": "decoder_output",
                "integrity": "unknown",
                "producer": "survey.rds",
                "limitations": [_PS_LIMITATION],
            }
        return specs

    # -- decode events ----------------------------------------------------

    def on_decode_event(self, event: dict) -> str | None:
        """Link a stored decode event (DecodeEventRepo row dict) to an
        observation. Returns the observation id, or None when the event has
        no frequency to place it by.

        `analysis_run_id` (review decode) takes a separate path: it is
        never treated as live (no capture-time start/end, no bump to a live
        encounter count) -- a re-analysis of a recording is not a new live
        reception, even when a live session happens to be open at the same
        time. Its events partition into one Observation per (source_id,
        generation, frequency): a run can visit several channels, and a seek
        bumps `generation`, so neither may merge into another channel's or
        an earlier pass's observation (fix: previously *all* of a run's
        events shared one observation, regardless of channel or seeks)."""
        event_id = event.get("id")
        if event_id is None:
            return None
        analysis_run_id = event.get("analysis_run_id")
        freq = event.get("frequency_hz")
        decoder_id = event.get("decoder")
        source_id = event.get("source_id") or (self.session.source_id if self.session else None)

        if analysis_run_id:
            sample_index = event.get("sample_index")
            generation = event.get("generation")
            partitions = self._analysis_obs.setdefault(analysis_run_id, [])
            part = next(
                (
                    p
                    for p in partitions
                    if p.source_id == source_id
                    and p.generation == generation
                    and _same_channel(p.freq, freq)
                ),
                None,
            )
            if part is None:
                obs = self.repo.add(
                    Observation(
                        session_id=self._session_id,
                        signal_id=event.get("signal_id"),
                        start_ts=None,
                        end_ts=None,
                        sample_start=sample_index,
                        sample_end=sample_index,
                        reported_hz=freq,
                        provenance="replay decode",
                        created_at=self._now(),
                        updated_at=self._now(),
                    )
                )
                part = _AnalysisPartition(
                    obs_id=obs.id, source_id=source_id, generation=generation, freq=freq
                )
                partitions.append(part)
                self._decode_obs[part.obs_id] = set()
                part.ref = self._add_recording_ref(obs.id, sample_index, event.get("run_index"))
            else:
                if sample_index is not None:
                    obs = self.repo.get(part.obs_id)
                    if obs is not None:
                        self.repo.update(obs.model_copy(update={"sample_end": sample_index}))
                self._extend_recording_ref(part, sample_index)
            self.repo.link_decode_event(event_id, part.obs_id)

            claimed = self._decode_obs.setdefault(part.obs_id, set())
            if decoder_id:
                claimed.add(decoder_id)
                self._upsert_decode_claim(
                    part.obs_id,
                    decoder_id,
                    Claim(
                        **_decode_claim_spec(decoder_id, event),
                        observation_id=part.obs_id,
                        source_id=source_id,
                        analysis_run_id=analysis_run_id,
                        sample_start=sample_index,
                        sample_end=sample_index,
                        support=_decode_support(event_id, event),
                        created_at=datetime.fromtimestamp(self._now(), UTC),
                    ),
                )
            return part.obs_id

        if freq is None:
            return None
        ts = event.get("ts") if self._live else None
        obs = self.repo.open_near(freq, DECODE_TOLERANCE_HZ, self._session_id)
        if obs is None:
            obs = self.repo.add(
                Observation(
                    session_id=self._session_id,
                    signal_id=event.get("signal_id"),
                    start_ts=ts,
                    end_ts=ts,
                    reported_hz=freq,
                    provenance="decode event",
                    created_at=self._now(),
                    updated_at=self._now(),
                )
            )
            self._decode_obs[obs.id] = set()
        elif obs.id in self._decode_obs and ts is not None and ts > (obs.end_ts or 0):
            self.repo.update(obs.model_copy(update={"end_ts": ts}))
        self.repo.link_decode_event(event_id, obs.id)

        claimed = self._decode_obs.get(obs.id)
        # Survey-label observations get their decoder claim via `confirmed`;
        # only survey-less ones get one here, from the first event per decoder.
        if claimed is not None and decoder_id:
            claimed.add(decoder_id)
            at = datetime.fromtimestamp(ts, UTC) if ts is not None else None
            self._upsert_decode_claim(
                obs.id,
                decoder_id,
                Claim(
                    **_decode_claim_spec(decoder_id, event),
                    observation_id=obs.id,
                    source_id=source_id,
                    observed_start=at,
                    observed_end=at,
                    support=_decode_support(event_id, event),
                    created_at=datetime.fromtimestamp(self._now(), UTC),
                ),
            )
        return obs.id

    def _upsert_decode_claim(self, obs_id: str, decoder_id: str, claim: Claim) -> None:
        key = (obs_id, decoder_id)
        current = self._decode_claims.get(key)
        if current is None:
            self._decode_claims[key] = self.repo.add_claim(claim)
            return
        rank = {"unknown": 0, "unavailable": 1, "failed": 2, "passed": 3}
        if rank.get(claim.integrity, 0) > rank.get(current.integrity, 0):
            self._decode_claims[key] = self.repo.supersede(current.claim_id, claim)

    def _add_recording_ref(
        self, obs_id: str, sample_index: int | None, run_index: int | None
    ) -> RecordingReference | None:
        """A replay decode observation's evidence includes *where on the
        recording* it came from -- same idea as a review bookmark's ref
        (`review_bookmark_create` in msdr.api.service). Run index and sample
        index together locate run-local samples in multi-run SigMF. None on a
        live session: there is no recording to point at."""
        if self.session is None or self.session.mode != "replay" or not self.session.origin:
            return None
        path = self.session.origin
        return self.repo.add_ref(
            RecordingReference(
                observation_id=obs_id,
                path=path,
                name=Path(path).name,
                run_index=run_index,
                sample_start=sample_index,
                sample_end=sample_index,
            )
        )

    def _extend_recording_ref(self, part: _AnalysisPartition, sample_index: int | None) -> None:
        if part.ref is None or sample_index is None:
            return
        part.ref = part.ref.model_copy(update={"sample_end": sample_index})
        self.repo.update_ref(part.ref)

    # -- scanner ----------------------------------------------------------

    def on_scanner_hit(self, hit: dict, find_id: str) -> Observation:
        """One closed observation for a scanner dwell. The on_find payload's
        `ts` is taken after the dwell, so the dwell started duration_s before."""
        ts = hit.get("ts")
        duration = hit.get("duration_s") or 0.0
        live = self._live and ts is not None
        measurements = []
        if hit.get("peak_snr_db") is not None:
            measurements.append(
                Measurement(
                    name="snr",
                    value=float(hit["peak_snr_db"]),
                    unit="dB",
                    method="scanner_peak_snr",
                    window_s=float(duration),
                )
            )
        demod = hit.get("demodulator")
        now = self._now()
        return self.repo.add(
            Observation(
                session_id=self._session_id,
                signal_id=hit.get("signal_id"),
                find_id=find_id,
                start_ts=ts - duration if live else None,
                end_ts=ts if live else None,
                reported_hz=hit.get("frequency_hz"),
                bandwidth_hz=hit.get("bandwidth_hz"),
                receiver={"demodulator": demod} if demod else None,
                measurements=measurements,
                provenance="scanner hit",
                closed=True,
                created_at=now,
                updated_at=now,
            )
        )


def _analyzer_support(label: dict) -> list[str]:
    """The `family`/voice claim's `support`: raw/snapped symbol rate,
    tolerance and modulation kind straight from the analyzer's own fields --
    never a percentage confidence. Falls back to the earlier single
    `symbol_rate_hz` reading when the analyzer hasn't set the new fields yet
    (an older label, or a test double)."""
    out: list[str] = []
    raw = label.get("symbol_rate_raw_hz")
    snapped = label.get("symbol_rate_snapped_hz")
    tolerance = label.get("symbol_rate_tolerance")
    if raw is not None:
        out.append(f"raw rate {float(raw):g} Hz")
    if snapped is not None:
        out.append(f"snapped rate {float(snapped):g} Hz")
    if tolerance is not None:
        out.append(f"tolerance {float(tolerance) * 100:g}%")
    if not out:
        # Earlier label (or a test double): only the single symbol_rate_hz
        # reading exists.
        out = _symbol_rate_support(label.get("symbol_rate_hz"))
    modulation_kind = (label.get("modulation") or {}).get("kind")
    if modulation_kind:
        out.append(f"modulation {modulation_kind}")
    return out


def _symbol_rate_support(rate) -> list[str]:
    # The label carries one number (snapped to a standard rate when within
    # 3 %, else the raw estimate); a dict form with both is accepted too.
    if rate is None:
        return []
    if isinstance(rate, dict):
        out = []
        if rate.get("raw") is not None:
            out.append(f"symbol rate raw {rate['raw']:g} Hz")
        if rate.get("snapped") is not None:
            out.append(f"symbol rate snapped {rate['snapped']:g} Hz")
        return out
    return [f"symbol rate {float(rate):g} Hz (snapped when within 3 % of a standard rate)"]
