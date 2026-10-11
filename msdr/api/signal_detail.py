"""GET /api/signal-detail (U3): one aggregate view of a frequency for the
signal-detail panel, reached from every "select a signal" surface (survey
label, find row, memory row, decode event, scanner hit, catalog row) -- see
docs/superpowers/plans/2026-09-26-ui-u3.md Stream A.

Aggregates memory (Signal), a scanner find, the nearest active survey label
(+-1 spectrum bin, msdr.api.service.AppService._survey_label_near's own
tolerance) and an evidence list built from decode events / label.confirmed
(decoded), memory (name/notes), catalog on-air matches (scheduled), the
band-plan match, and the analyzer's modulation/family estimate. Never raises
for a missing part -- a disabled feature, no active survey, or an unknown
`signal_id`/`find_id` just leaves that part null/empty. The one exception:
when `frequency_hz` is omitted and neither id resolves to anything, there is
nothing to look up, so this raises the 404 the route surfaces.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from msdr.api.services.allocations import allocation_summary, format_services

if TYPE_CHECKING:
    from msdr.api.service import AppService

# Same tolerance _is_find_ignored uses for "near this frequency" (S11).
_DECODE_MESSAGE_TOLERANCE_HZ = 6000
_MESSAGE_LIMIT = 20
# Bound on how far back through decode_events (newest-first) to look for
# messages within tolerance -- generous relative to _MESSAGE_LIMIT so a busy
# frequency's messages aren't crowded out by unrelated ones in between.
_DECODE_EVENT_SCAN_LIMIT = 2000
# Matches _catalog_candidates' own on-air/off-air confidence split -- kept as
# the (internal, unshown) on-air filter threshold; see _scheduled_and_bandplan_evidence.
_ON_AIR_CONFIDENCE = 0.85
_DEFAULT_BANDWIDTH_HZ = 12_500
_OBSERVATIONS_NEAR_LIMIT = 10


def _last_classification(observations: list[dict], current: dict | None) -> dict | None:
    """Recover useful recorded estimates, including superseded claims."""
    results = [current] if current else []
    for observation in observations:
        for claim in observation.get("claims", []):
            value = claim.get("value", "")
            if (claim.get("producer") != "survey.analyzer"
                    or claim.get("analysis_run_id")
                    or not (claim.get("subject") == "protocol"
                            or value == "keyed carrier"
                            or "speech" in value.lower() or "voice" in value.lower()
                            or value == "data-like")):
                continue
            at = claim.get("observed_start")
            if not at:
                continue
            try:
                timestamp = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            results.append({"name": value, "at": timestamp, "source": "analyzer"})
    return max(results, key=lambda item: item["at"], default=None)


def _rds_evidence(survey_label: dict | None) -> list[dict]:
    """PI (validated_decode/validated) and PS (decoder_output/estimated) are
    two separate claims, never merged into one confidence number (S2)."""
    rds = (survey_label or {}).get("rds")
    if not rds:
        return []
    at = (survey_label or {}).get("last_seen")
    items: list[dict] = []
    pi = rds.get("pi")
    if pi and (rds.get("groups") or 0) > 0:
        items.append(
            {
                "kind": "decoded",
                "title": f"PI {pi}",
                "detail": rds.get("pty_name") or "",
                "evidence_type": "validated_decode",
                "state": "validated",
                "score": None,
                "source": "rds",
                "at": at,
            }
        )
    ps = rds.get("ps")
    if ps:
        parts = []
        if rds.get("pty_name"):
            parts.append(rds["pty_name"])
        if rds.get("rt"):
            parts.append(rds["rt"])
        items.append(
            {
                "kind": "decoded",
                "title": f"RDS: {ps}",
                "detail": " · ".join(parts),
                "evidence_type": "decoder_output",
                "state": "estimated",
                "score": None,
                "source": "rds",
                "at": at,
            }
        )
    return items


def _state_for_integrity(integrity: str | None, decoder_id: str | None) -> tuple[str, str]:
    """(evidence_type, state) from a specific event's own `integrity` field
    (S4) when it has one: passed -> validated_decode/validated, anything else
    (failed/unknown/unavailable/not_applicable) -> decoder_output/estimated.
    Missing integrity is unknown, never proof that a check passed."""
    if integrity:
        if integrity == "passed":
            return "validated_decode", "validated"
        return "decoder_output", "estimated"
    return "decoder_output", "estimated"


def _decoded_evidence(messages: list[dict], survey_label: dict | None) -> list[dict]:
    items: list[dict] = _rds_evidence(survey_label)
    if messages:
        newest = messages[0]
        evidence_type, state = _state_for_integrity(newest.get("integrity"), newest.get("decoder"))
        items.append(
            {
                "kind": "decoded",
                "title": f"Decoded ({newest.get('decoder')})",
                "detail": newest.get("text") or "",
                "evidence_type": evidence_type,
                "state": state,
                "score": None,
                "source": newest.get("decoder") or "decoder",
                "at": newest.get("ts"),
                "integrity": newest.get("integrity"),
                "integrity_note": newest.get("integrity_note"),
            }
        )
    elif not items:
        confirmed = (survey_label or {}).get("confirmed")
        if confirmed:
            evidence_type, state = _state_for_integrity(
                confirmed.get("integrity"), confirmed.get("decoder")
            )
            items.append(
                {
                    "kind": "decoded",
                    "title": f"Decoded ({confirmed.get('decoder')})",
                    "detail": confirmed.get("integrity_sample") or confirmed.get("sample") or "",
                    "evidence_type": evidence_type,
                    "state": state,
                    "score": None,
                    "source": confirmed.get("decoder") or "decoder",
                    "at": confirmed.get("at"),
                    "integrity": confirmed.get("integrity") or "unknown",
                }
            )
    return items


def _memory_evidence(memory: Any) -> dict | None:
    if memory is None:
        return None
    # A saved memory is an operator record; it only reads as "operator"
    # certainty once the operator has actually identified it (S2) -- an
    # unidentified memory is just context, same as an allocation/schedule hit.
    identified = getattr(memory, "status", None) == "identified"
    return {
        "kind": "memory",
        "title": memory.name,
        "detail": memory.notes or "",
        "evidence_type": "operator",
        "state": "operator" if identified else "context",
        "score": None,
        "source": "memory",
        "at": None,
    }


def _scheduled_and_bandplan_evidence(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    catalog_candidates = [c for c in candidates if c.get("provider") == "catalog"]
    scheduled = [
        {
            "kind": "scheduled",
            "title": c.get("name"),
            "detail": c.get("reason"),
            "evidence_type": "schedule",
            "state": "context",
            "score": None,
            "source": "catalog",
            "at": None,
        }
        for c in catalog_candidates
        # Internal on-air filter threshold -- not shown as a score (S2).
        if (c.get("confidence") or 0) >= _ON_AIR_CONFIDENCE
    ][:3]
    # Every band-plan candidate (docs/allocations.md: these are channel /
    # application hints now; the allocation itself is _allocation_evidence).
    bandplan = [
        {
            "kind": "bandplan",
            "title": c.get("name"),
            "category": c.get("category"),
            "detail": c.get("reason"),
            "evidence_type": "allocation",
            "state": "context",
            "score": None,
            "source": "bandplan",
            "at": None,
        }
        for c in candidates
        if c.get("provider") == "bandplan"
    ]
    return scheduled, bandplan


def _allocation_evidence(context: dict) -> dict:
    """One item for the full allocation lookup: every service of the band,
    primary first, secondary marked; `detail` carries the whole context."""
    title = format_services(allocation_summary(context)["services"])
    if not title:
        title = context.get("message") or context.get("status") or "allocation"
    return {
        "kind": "allocation",
        "title": title,
        "category": None,
        "detail": context,
        "evidence_type": "allocation",
        "state": "context",
        "score": None,
        "source": "allocations",
        "at": None,
    }


def _candidate_support(survey_label: dict) -> list[str]:
    """The candidate evidence item's `support` (S4): raw/snapped symbol rate,
    tolerance and modulation kind, from the analyzer's own fields -- never a
    percentage, and never invented when the analyzer hasn't set a field yet
    (pre-S4 label: every one of these is simply absent)."""
    out: list[str] = []
    raw = survey_label.get("symbol_rate_raw_hz")
    snapped = survey_label.get("symbol_rate_snapped_hz")
    tolerance = survey_label.get("symbol_rate_tolerance")
    if raw is not None:
        out.append(f"raw rate {float(raw):g} Hz")
    if snapped is not None:
        out.append(f"snapped rate {float(snapped):g} Hz")
    if tolerance is not None:
        out.append(f"tolerance {float(tolerance) * 100:g}%")
    modulation_kind = (survey_label.get("modulation") or {}).get("kind")
    if modulation_kind:
        out.append(f"modulation {modulation_kind}")
    return out


def _analyzer_evidence(survey_label: dict | None) -> dict | None:
    if not survey_label:
        return None
    at = survey_label.get("analyzed_at")
    family = survey_label.get("family")
    if family:
        return {
            "kind": "analyzer",
            "title": family.get("name"),
            "detail": ", ".join(family.get("suggested_decoders") or []),
            "evidence_type": "iq_estimate",
            "state": "estimated",
            "score": family.get("confidence"),
            "source": "analyzer",
            "at": at,
            "support": _candidate_support(survey_label),
            "alternatives": list(family.get("alternatives") or []),
            "next_action": family.get("next_action") or "",
        }
    modulation = survey_label.get("modulation")
    if modulation and modulation.get("kind") not in (None, "unknown"):
        return {
            "kind": "analyzer",
            "title": f"{modulation['kind']} (analyzer estimate)",
            "detail": "",
            "evidence_type": "iq_estimate",
            "state": "estimated",
            "score": modulation.get("confidence"),
            "source": "analyzer",
            "at": at,
        }
    return None


def _suggested_decoders(memory: Any, survey_label: dict | None, messages: list[dict]) -> list[str]:
    if memory is not None and memory.decoders:
        return list(memory.decoders)
    # A decoder that already produced messages here is the strongest hint --
    # without this, DECODE on a decoded-but-unsaved frequency had nothing to start.
    decoded_ids = list(dict.fromkeys(m["decoder"] for m in messages if m.get("decoder")))
    if decoded_ids:
        return decoded_ids
    confirmed = (survey_label or {}).get("confirmed")
    if confirmed and confirmed.get("decoder"):
        return [confirmed["decoder"]]
    family = (survey_label or {}).get("family")
    if family and family.get("suggested_decoders"):
        return list(family["suggested_decoders"])
    return []


def build_signal_detail(
    service: AppService,
    *,
    frequency_hz: int | None,
    signal_id: str | None,
    find_id: str | None,
) -> dict:
    memory = service.signals.get(signal_id) if signal_id is not None else None
    find = service.finds.get(find_id) if find_id is not None else None

    if frequency_hz is None:
        if memory is not None:
            frequency_hz = memory.frequency_hz
        elif find is not None:
            frequency_hz = find["frequency_hz"]
        else:
            raise HTTPException(404, "signal not found")
    elif memory is None and signal_id is None:
        # No signal_id was given at all (as opposed to one that didn't
        # resolve) -- fall back to the nearest memory, same radius as the
        # rest of the app's "signal near this frequency" lookups.
        near = service.signals.near(frequency_hz, service.settings.nearby_hz)
        memory = near[0] if near else None

    survey_label = service._survey_label_near(frequency_hz)

    raw_events = service.decode_events.list(limit=_DECODE_EVENT_SCAN_LIMIT)
    messages = [
        event
        for event in raw_events
        if event.get("frequency_hz") is not None
        and abs(event["frequency_hz"] - frequency_hz) <= _DECODE_MESSAGE_TOLERANCE_HZ
    ][:_MESSAGE_LIMIT]

    identify_bw_hz = (
        (memory.filter_bandwidth_hz if memory is not None else None)
        or (find.get("bandwidth_hz") if find is not None else None)
        or int(service.confirmed.props.get("filter_bandwidth") or _DEFAULT_BANDWIDTH_HZ)
    )
    # Same call _classify_find makes -- never raises, [] when ident/catalog
    # are both off (S11 contract), regardless of whether a survey is active.
    candidates = service._identify_for_survey(frequency_hz, identify_bw_hz)

    evidence: list[dict] = []
    evidence.extend(_decoded_evidence(messages, survey_label))
    memory_evidence = _memory_evidence(memory)
    if memory_evidence is not None:
        evidence.append(memory_evidence)
    scheduled, bandplan = _scheduled_and_bandplan_evidence(candidates)
    evidence.extend(scheduled)
    evidence.extend(bandplan)
    evidence.append(_allocation_evidence(service.allocation_context(frequency_hz)))
    analyzer = _analyzer_evidence(survey_label)
    if analyzer is not None:
        evidence.append(analyzer)

    for item in evidence:
        item.setdefault("source_group", {
            "analyzer": "dsp", "decoded": "decoder", "memory": "memory",
            "bandplan": "context", "scheduled": "context", "allocation": "context",
        }.get(item.get("kind"), "other"))

    nearby_observations = service.observations.list(
        frequency_hz=frequency_hz, limit=_OBSERVATIONS_NEAR_LIMIT
    )
    observations = [service.observations.summary(o) for o in nearby_observations]

    return {
        "frequency_hz": frequency_hz,
        "memory": memory.model_dump(mode="json") if memory is not None else None,
        "find": find,
        "survey_label": survey_label,
        "observations": observations,
        "last_classification": _last_classification(
            observations, (survey_label or {}).get("last_classification")
        ),
        "evidence": evidence,
        "messages": messages,
        "suggested_decoders": _suggested_decoders(memory, survey_label, messages),
        "demodulator": (memory.demodulator if memory is not None else None)
        or (find.get("demodulator") if find is not None else None),
        "bandwidth_hz": (memory.filter_bandwidth_hz if memory is not None else None)
        or (find.get("bandwidth_hz") if find is not None else None),
    }
