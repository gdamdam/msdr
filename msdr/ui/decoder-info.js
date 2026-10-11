const FALLBACK_CATEGORIES = {
  pocsag: "Paging",
  flex: "Paging",
  eas: "Emergency alerts",
  aprs: "Packet & tracking",
  lora: "LoRa & mesh",
  packet9600: "Packet & tracking",
  packet300: "Packet & tracking",
  ais: "Packet & tracking",
  dtmf: "Tone signaling",
  cw: "Telegraphy",
  rtty: "HF text & data",
  rtty_minimodem: "HF text & data",
  navtex: "HF text & data",
  wefax: "Facsimile",
  wwv: "Time signals",
  dcf77: "Time signals",
  msf: "Time signals",
  dmr: "Digital voice",
  p25: "Digital voice",
  nxdn: "Digital voice",
  nxdn96: "Digital voice",
  dstar: "Digital voice",
  ysf: "Digital voice",
  dpmr: "Digital voice",
  m17: "Digital voice",
  adsb: "Aviation surveillance",
  rs41: "Weather balloons",
  ft8: "HF weak-signal modes",
  dsc: "Maritime signaling",
  endoftrain: "Rail telemetry",
  vor: "Aviation navigation",
  ils: "Aviation navigation",
};

const FALLBACK_DESCRIPTIONS = {
  lora: "LoRa IQ reception using configurable SF, bandwidth and coding rate. Optional GNU Radio PHY runtime; mesh protocol headers and opaque encrypted payloads retain provenance. No native decryption or companion radio required.",
  pocsag: "NFM paging decoder for POCSAG 512/1200/2400. Output includes capcode, function, and message text.",
  flex: "NFM paging decoder for FLEX bursts. Output includes capcode, baud/frame facts, and recovered message text.",
  eas: "NFM audio decoder for Emergency Alert System SAME headers. Output includes event, location, and originator fields when present.",
  aprs: "NFM/AFSK decoder for 1200-baud AX.25 (APRS). Output is packet text with source/destination callsigns and any position or telemetry fields.",
  packet9600: "IQ discriminator decoder for 9600-baud G3RUH AX.25. It needs flat discriminator samples; output is AX.25 packet text.",
  packet300: "USB/LSB decoder for 300-baud AX.25, commonly used on HF. Output is AX.25 packet text.",
  ais: "IQ discriminator decoder for AIS GMSK on NFM. Output contains vessel identity, position, course, speed, and related AIS fields when sent.",
  dtmf: "NFM audio detector for dual-tone multi-frequency signaling. Output is the sequence of detected keypad digits.",
  cw: "Audio decoder for Morse telegraphy. Output is the recovered character stream and signal-quality facts.",
  rtty: "USB/LSB FSK decoder for radioteletype. Built in; automatically estimates mark/space tones, shift and baud rate (common rates and arbitrary), locking once stable, with reversed-shift (inverted) detection. Output is recovered text plus the detected baud, shift and confidence.",
  rtty_minimodem: "USB/LSB FSK decoder for radioteletype via the optional minimodem binary, as a cross-check against the built-in native 'RTTY' decoder. Output is recovered text; tune the signal's shift and baud rate into the filter passband.",
  navtex: "USB decoder for NAVTEX SITOR-B at 100 baud. Output includes station/message headers and recovered maritime text.",
  wefax: "USB radiofax image decoder. Tune about 1.9 kHz below the listed fax frequency; output is a fax image with line and phasing controls.",
  wwv: "AM decoder for WWV/WWVH time-code markers. Output includes the recovered minute/time fields and synchronization state.",
  dcf77: "AM decoder for the DCF77 one-minute time-code pattern. Output includes decoded date/time fields and synchronization state.",
  msf: "AM decoder for the MSF time-code pattern. Output includes decoded date/time fields and synchronization state.",
  dmr: "IQ/NFM decoder for DMR digital voice signaling. Output includes color code, slot, talkgroup, and call events; supported voice may be rendered when available.",
  p25: "IQ/NFM decoder for P25 Phase 1. Output includes NAC, talkgroup/unit identifiers, and call events; supported voice may be rendered when available.",
  nxdn: "IQ/NFM decoder for NXDN48 (6.25 kHz). Output includes RAN, talkgroup/unit identifiers, and call events; supported voice may be rendered when available.",
  nxdn96: "IQ/NFM decoder for NXDN96 (12.5 kHz). Output includes RAN, talkgroup/unit identifiers, and call events; supported voice may be rendered when available.",
  dstar: "IQ/NFM decoder for D-STAR. Output includes callsign routing, repeater, and slow-data fields with decoded voice when supported.",
  ysf: "IQ/NFM decoder for Yaesu System Fusion. Output includes frame type, callsign, and routing fields with decoded voice when supported.",
  dpmr: "IQ/NFM decoder for dPMR. Output includes color code, slot, identifiers, and call state with decoded voice when supported.",
  m17: "IQ/NFM decoder for M17. Output includes callsign, stream/frame state, and supported voice data.",
  adsb: "IQ decoder for 1090 MHz ADS-B. Output includes aircraft address, callsign, position, altitude, speed, and decoded frame facts when transmitted.",
  rs41: "IQ decoder for RS41 radiosondes. Output includes frame identity, GPS position, altitude, and sensor telemetry when present.",
  ft8: "FT8 weak-signal exchanges via WSJT-X jt9. Tunes a 3 kHz USB channel and decodes 15-second windows; output includes callsigns, grid locators and exchange text. Requires IQ at an integer multiple of 12 kHz.",
  dsc: "Digital Selective Calling: 100-baud MF/HF FSK or 1200-baud VHF channel 70. Built-in IQ decoding with XOR ECC; supported calls expose MMSI, category and valid distress position/time. Unsupported layouts remain raw symbols.",
  endoftrain: "IQ decoder for end-of-train telemetry. Output includes radio ID, marker/status, and telemetry fields when present.",
  vor: "IQ analyzer for VOR navigation signals. Output includes radial/bearing and signal-quality measurements.",
  ils: "IQ analyzer for ILS localizer and glideslope signals. Output includes deviation and signal-quality measurements.",
};

export function decoderCategory(decoder) {
  if (decoder?.category) return decoder.category;
  const byId = FALLBACK_CATEGORIES[decoder?.id];
  if (byId) return byId;
  if (decoder?.kind === "call") return "Digital voice";
  if (decoder?.kind === "image") return "Facsimile";
  if (decoder?.kind === "time") return "Time signals";
  if ((decoder?.demodulators ?? []).some((mode) => ["USB", "LSB"].includes(mode))) return "HF text & data";
  return "VHF/UHF data";
}

export function decoderDescription(decoder) {
  if (decoder?.description) return decoder.description;
  if (FALLBACK_DESCRIPTIONS[decoder?.id]) return FALLBACK_DESCRIPTIONS[decoder.id];
  const modes = (decoder?.demodulators ?? []).join(" / ") || "the current mode";
  const path = decoder?.iq_only ? " IQ input" : " audio input";
  return `Decodes ${decoder?.label ?? decoder?.id ?? "this protocol"} using ${modes}${path}. Results can be incomplete or misdecoded in weak or noisy reception; treat them as receiver output, not verified facts.`;
}

/** Display the evidence behind a decoder's validation record without
 * implying that a reference fixture proves off-air performance. */
export function decoderValidation(decoder) {
  const readiness = decoder?.readiness;
  const state = readiness?.validation?.state ?? "untested";
  const stale = !!readiness?.validation_stale;
  const labels = {
    untested: ["untested", "No decoder validation is recorded."],
    fixture_tested: ["fixture-tested", "Synthetic or built-in fixtures only; off-air performance is unverified."],
    reference_iq_tested: ["reference IQ-tested", "Tested with a reference recording; off-air performance may still be unverified."],
    live_output_observed: ["live output", "Decoded output observed off-air; independent content comparison is pending."],
    field_observed: ["off-air observed", "Observed off-air with an independent reference."],
    regression_detected: ["regression detected", "A regression is recorded for this decoder."],
  };
  const [text, title] = labels[state] ?? labels.untested;
  return {
    text: stale ? `${text} (stale)` : text,
    title: stale ? `${title} Validation record is stale for the installed backend.` : title,
  };
}

function decoderIsExperimental(decoder) {
  const state = decoder?.readiness?.validation?.state ?? "untested";
  return !!decoder?.readiness?.validation_stale ||
    !["reference_iq_tested", "field_observed"].includes(state);
}

export function groupDecoders(decoders = []) {
  const groups = new Map();
  for (const decoder of decoders) {
    const category = decoderIsExperimental(decoder) ? "Experimental" : decoderCategory(decoder);
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(decoder);
  }
  return [...groups].map(([category, items]) => ({ category, decoders: items }));
}
