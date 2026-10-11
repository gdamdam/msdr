/** Survey-table evidence categories. A decoder capability or a decoder event
 * alone is not a passed check. RDS PI is validated only with checked groups,
 * matching the observation recorder and inspector's PI evidence contract. */
const EVIDENCE_RANK = {
  none: 0,
  context: 1,
  estimate: 2,
  decoder_output: 3,
  validated: 4,
};

export function surveyEvidence(label = {}) {
  if (label.confirmed?.integrity === "passed" || (label.rds?.pi && label.rds.groups > 0)) {
    return { category: "validated", rank: EVIDENCE_RANK.validated, text: "validated · passed", kind: "decoded" };
  }
  if (label.confirmed) {
    return { category: "decoder_output", rank: EVIDENCE_RANK.decoder_output, text: "decoder output", kind: "" };
  }
  if (label.rds?.pi || label.rds?.ps || label.rds?.callsign) {
    return { category: "decoder_output", rank: EVIDENCE_RANK.decoder_output, text: "decoder output", kind: "" };
  }
  if (label.family?.name || label.modulation?.kind || label.voice?.kind ||
      label.decoder_candidates?.some((c) => c.evidence_kind === "iq_estimate")) {
    return { category: "estimate", rank: EVIDENCE_RANK.estimate, text: "estimate", kind: "analyzer" };
  }
  const hasCatalog = (label.catalog?.length ?? 0) > 0;
  if (hasCatalog || label.bandplan?.name || label.allocation ||
      label.decoder_candidates?.some((c) => c.evidence_kind === "allocation")) {
    return {
      category: "context",
      rank: EVIDENCE_RANK.context,
      text: "context",
      kind: hasCatalog ? "scheduled" : label.allocation ? "allocation" : "bandplan",
    };
  }
  return { category: "none", rank: EVIDENCE_RANK.none, text: "—", kind: "" };
}

export function matchesEvidenceFilter(label, filter = "all") {
  return filter === "all" || surveyEvidence(label).category === filter;
}

export function compareEvidence(a, b, order = "strongest") {
  const difference = surveyEvidence(a).rank - surveyEvidence(b).rank;
  return order === "weakest" ? difference : -difference;
}


// Source colors are shared by DOM badges and canvas markers. Color never
// represents probability; integrity and disagreement have separate symbols.
export const EVIDENCE_SOURCES = {
  dsp: {text: "Measured", code: "DSP", color: "#45d5e0"},
  ml: {text: "Experimental ML", code: "ML", color: "#bb9aff"},
  allocation: {text: "Allocation", code: "A", color: "#92afd5"},
  catalog: {text: "Scheduled", code: "S", color: "#efc16b"},
  decoded: {text: "Decoded", code: "D", color: "#64ddbb"},
  operator: {text: "Operator", code: "O", color: "#eeeeee"},
};

export function evidenceBadges(label = {}) {
  const badges = [];
  const add = (source, symbol, title, text) => badges.push({
    ...EVIDENCE_SOURCES[source], source, symbol, title,
    ...(text ? {text} : {}),
  });
  if (!label.ml_only && (Number.isFinite(label.snr_db) || Number.isFinite(label.width_hz) ||
      label.family?.name || label.modulation?.kind || label.voice?.kind ||
      label.decoder_candidates?.some(c => c.evidence_kind === "iq_estimate"))) {
    add("dsp", "≈", "DSP measurements / estimates. Physical signal evidence; modulation and voice inferences remain tentative.");
  }
  // Keep detector proposals and classifier outputs distinct. These badges are
  // advisory only: neither changes evidence ranking or establishes a signal.
  const detection = label.rfml_detection;
  const detectionEvidence = detection?.evidence ?? detection;
  if (detection && Number.isFinite(detection.score)) {
    const name = typeof detectionEvidence?.label === "string" && detectionEvidence.label.trim()
      ? detectionEvidence.label.trim() : "signal proposal";
    const score = detection.score.toFixed(3);
    const historical = detection.stale === true || label._goneAt != null;
    const age = label._goneAt == null && Number.isFinite(detection.age_s) ? ` · ${Math.round(detection.age_s)} s old` : "";
    const freshness = historical ? `Historical result${age}` : `Current result${age}`;
    const text = historical ? "ML detect · historical" : "ML detect · current";
    add("ml", "?", `Experimental ML detector proposal (${name}). ${freshness}; raw score ${score} is uncalibrated, not a probability.`, text);
  }
  const classification = label.rfml;
  const className = typeof classification?.classification === "string"
    ? classification.classification.trim() : "";
  if (classification?.status === "ok" && className && className.toLowerCase() !== "unknown") {
    const score = Number.isFinite(classification.score) ? ` · ${classification.score.toFixed(3)}` : "";
    const disagreement = classification.comparison === "disagreement";
    const symbol = disagreement ? "⚠" : "?";
    const disagreementNote = disagreement ? " Classifier disagrees with the DSP estimate." : "";
    if (classification.stale === true || label._goneAt != null) {
      const age = label._goneAt == null && Number.isFinite(classification.age_s) ? ` · ${Math.round(classification.age_s)} s old` : "";
      add("ml", symbol, `Experimental ML classifier output: ${className}. Historical result${age}; raw score${score || " unavailable"} is uncalibrated, not a probability.${disagreementNote}`, `ML class · historical · ${className}`);
    } else {
      const age = label._goneAt == null && Number.isFinite(classification.age_s) ? ` · ${Math.round(classification.age_s)} s old` : "";
      add("ml", symbol, `Experimental ML classifier output: ${className}. Current result${age}; raw score${score || " unavailable"} is uncalibrated, not a probability.${disagreementNote}`, `ML class · ${className}`);
    }
  }
  if (label.allocation || label.bandplan?.name || label.decoder_candidates?.some(c => c.evidence_kind === "allocation"))
    add("allocation", "?", "Frequency allocation only; does not establish the received service or station.");
  if (label.catalog?.length)
    add("catalog", "?", "Catalog / schedule match only; not evidence that this station was received.");
  if (label.confirmed) {
    const integrity = label.confirmed.integrity;
    add("decoded", integrity === "passed" ? "✓" : integrity === "failed" ? "⚠" : "?",
      `Decoder ${label.confirmed.decoder || "output"}: integrity ${integrity || "unknown"}. Passed checks validate the payload, not every inferred identity.`,
      label.confirmed.decoder === "cw" ? "CW output" : undefined);
  }
  if (label.rds?.pi || label.rds?.ps || label.rds?.callsign) {
    const checked = label.rds.pi && label.rds.groups > 0;
    add("decoded", checked ? "✓" : "?", checked ? "RDS PI from checked groups." : "RDS output; checked PI groups unavailable.", "RDS decoded");
  }
  // Do not infer an operator identification from a catalog or memory name.
  if (label.claims?.some(c => c.evidence_type === "operator" && !c.superseded_by))
    add("operator", "", "Explicit operator identification; a human assertion, not an automatic decoder check.");
  return badges;
}

/** Keep exact frequency anchors; displace only colliding text stacks.
 * If the viewport cannot fit every stack, merge the nearest pair but retain
 * every original anchor so the shared label never implies a shifted signal. */
export function clusteredEvidenceMarkers(labels, lowHz, spanHz, widthPx, cellPx = 28) {
  if (![lowHz, spanHz, widthPx, cellPx].every(Number.isFinite) ||
      !(spanHz > 0) || !(widthPx > 0) || !(cellPx > 0)) return [];
  const markers = (labels ?? []).map(label => {
    const anchorHz = Number.isFinite(label.peak_frequency_hz) ? label.peak_frequency_hz : label.frequency_hz;
    const x = (anchorHz - lowHz) / spanHz * widthPx;
    return {
      opacity: label._markerOpacity ?? 1,
      anchor_styles: [{opacity: label._markerOpacity ?? 1, retained: !!label._markerRetained, level: label.level}],
      x, anchors: [x], anchor_frequencies_hz: [label.frequency_hz],
      count: 1, sources: [...new Map(evidenceBadges(label).map(source => [source.source, source])).values()],
    };
  }).filter(m => Number.isFinite(m.x) && m.x >= 0 && m.x <= widthPx && m.sources.length)
    .sort((a, b) => a.x - b.x);
  const capacity = Math.max(1, Math.floor(widthPx / cellPx));
  while (markers.length > capacity) {
    let closest = 0;
    for (let i = 1; i < markers.length - 1; i++) {
      if (markers[i + 1].x - markers[i].x < markers[closest + 1].x - markers[closest].x) closest = i;
    }
    const a = markers[closest], b = markers[closest + 1];
    const count = a.count + b.count;
    markers.splice(closest, 2, {
      x: (a.x * a.count + b.x * b.count) / count, count,
      opacity: Math.max(a.opacity, b.opacity),
      anchor_styles: [...a.anchor_styles, ...b.anchor_styles],
      anchors: [...a.anchors, ...b.anchors],
      anchor_frequencies_hz: [...a.anchor_frequencies_hz, ...b.anchor_frequencies_hz],
      sources: [...new Map([...a.sources, ...b.sources].map(s => [s.source, s])).values()],
    });
  }
  const margin = Math.min(cellPx / 2, widthPx / 2);
  for (let i = 0; i < markers.length; i++) {
    markers[i].x = Math.max(margin, markers[i].x, i ? markers[i - 1].x + cellPx : 0);
  }
  if (markers.length) markers[markers.length - 1].x = Math.min(markers.at(-1).x, widthPx - margin);
  for (let i = markers.length - 2; i >= 0; i--) {
    markers[i].x = Math.min(markers[i].x, markers[i + 1].x - cellPx);
  }
  return markers;
}

export function mlWorkerPhase(worker, hasResult = false) {
  if (!worker || ["disabled", "stopped"].includes(worker.state)) return "OFF";
  if (["unavailable", "error"].includes(worker.state)) return "ERROR";
  if (worker.state === "loading") return "LOADING";
  if (worker.state === "analyzing" || worker.queue > 0) return "ANALYZING";
  if (hasResult || (worker.completed > 0 && Number.isFinite(worker.last_result_age_s))) return "RESULT RECEIVED";
  return "WAITING FOR IQ";
}
