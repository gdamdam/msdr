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
  jjy: "Time signals",
  wwvb: "Time signals",
  chu: "Time signals",
  psk31: "HF text & data",
  ale: "HF text & data",
  dmr: "Digital voice",
  p25: "Digital voice",
  nxdn: "Digital voice",
  nxdn96: "Digital voice",
  dstar: "Digital voice",
  ysf: "Digital voice",
  dpmr: "Digital voice",
  m17: "Digital voice",
  adsb: "Aviation surveillance",
  acars: "Aviation surveillance",
  rs41: "Weather balloons",
  orbcomm: "Satellite data",
  rtl433: "ISM sensors & meters",
  rtl433_wmbus: "ISM sensors & meters",
  freedv700d: "HF digital voice",
  freedv700e: "HF digital voice",
  freedv1600: "HF digital voice",
  sonde_dfm: "Weather balloons",
  sonde_m10: "Weather balloons",
  sonde_m20: "Weather balloons",
  sonde_rs92: "Weather balloons",
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
  acars: "AM decoder for VHF ACARS (131.550, 131.725, 131.825 MHz and others). Output includes aircraft registration, flight id when present, label, block id and message text, with CRC and parity checked.",
  navtex: "USB decoder for NAVTEX SITOR-B at 100 baud. Output includes station/message headers and recovered maritime text.",
  wefax: "USB radiofax image decoder. Tune about 1.9 kHz below the listed fax frequency; output is a fax image with line and phasing controls.",
  wwv: "AM decoder for WWV/WWVH time-code markers. Output includes the recovered minute/time fields and synchronization state.",
  dcf77: "AM decoder for the DCF77 one-minute time-code pattern. Output includes decoded date/time fields and synchronization state.",
  msf: "AM decoder for the MSF time-code pattern. Output includes decoded date/time fields and synchronization state.",
  jjy: "AM decoder for the JJY (Japan, 40/60 kHz) one-minute time code. Output includes the decoded UTC time, parity result and offset from the host clock.",
  wwvb: "AM decoder for the WWVB (60 kHz) one-minute time code. Output includes the decoded UTC time, DUT1 and offset from the host clock.",
  chu: "USB decoder for CHU (Canada: 3330, 7850, 14670 kHz) Bell 103 time bursts in seconds 31-39. Output includes the decoded UTC time, validated by the burst's repeated copy.",
  psk31: "USB decoder for BPSK31 (31.25 baud, varicode). It finds the strongest carrier in the passband and tracks it; output is the recovered text, a line at a time, with the carrier frequency.",
  ale: "USB decoder for HF Automatic Link Establishment (2G, MIL-STD-188-141A): 8-FSK at 125 baud with Golay FEC. Output is the call, such as TO X TIS Y or TWAS ABC123, with call type and addresses.",
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
  rtl433: "IQ decoder for 315/433/868/915 MHz ISM devices (weather stations, TPMS, remotes) via the optional rtl_433 program. Output is model, id, channel and readings such as temperature, humidity and battery.",
  rtl433_wmbus: "IQ decoder for Wireless M-Bus meter telegrams (modes S and T, around 868 MHz) via rtl_433. Needs an IQ rate of 1 MS/s or more.",
  freedv700d: "USB decoder for FreeDV 700D via the optional freedv_rx program. Shows sync state, SNR and received text; decoded speech is not played.",
  freedv700e: "USB decoder for FreeDV 700E via the optional freedv_rx program. Shows sync state, SNR and received text; decoded speech is not played.",
  freedv1600: "USB decoder for FreeDV 1600 via the optional freedv_rx program. Shows sync state and SNR; decoded speech is not played.",
  sonde_dfm: "IQ decoder for Graw DFM-06/09/17 radiosondes via radiosonde_auto_rx's dfm09mod. Output includes serial, position, altitude and telemetry; shown on the Map like RS41.",
  sonde_m10: "IQ decoder for Meteomodem M10 radiosondes via radiosonde_auto_rx's m10mod. Output includes serial, position, altitude and telemetry; shown on the Map like RS41.",
  sonde_m20: "IQ decoder for Meteomodem M20 radiosondes via radiosonde_auto_rx's m20mod. Output includes serial, position, altitude and telemetry; shown on the Map like RS41.",
  sonde_rs92: "IQ decoder for Vaisala RS92 radiosondes via radiosonde_auto_rx's rs92mod. Position needs an ephemeris file msdr does not supply, so expect telemetry without a map position.",
  ft8: "FT8 weak-signal exchanges via WSJT-X jt9. Tunes a 3 kHz USB channel and decodes 15-second windows; output includes callsigns, grid locators and exchange text. Requires IQ at an integer multiple of 12 kHz.",
  dsc: "Digital Selective Calling: 100-baud MF/HF FSK or 1200-baud VHF channel 70. Built-in IQ decoding with XOR ECC; supported calls expose MMSI, category and valid distress position/time. Unsupported layouts remain raw symbols.",
  orbcomm: "IQ decoder for Orbcomm satellite downlinks (137-138 MHz, 4800 bps). Output includes packet type, satellite id and, for ephemeris packets, the satellite position. Packet layout is unvalidated on air.",
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
    !["reference_iq_tested", "live_output_observed", "field_observed"].includes(state);
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

/* Decode setup's status columns (decode-deck.js): one symbol per readiness
 * fact, explained on hover and in each column's (?) legend. */
const INSTALL_SYMBOLS = {
  builtin: ["✓", "ok", "Built in: ships with msdr, nothing to install."],
  present: ["✓", "ok", "Installed: the external program it needs was found."],
  incompatible: ["⊘", "warn", "Installed, but this version can't be used."],
  missing: ["✗", "bad", "Not installed: the external program it needs wasn't found."],
};
// Three levels on purpose: ○ untested, ◐ partly (fixtures, a reference
// recording, or validated on an older version), ● seen working off-air.
function testSymbol(state, stale) {
  if (state === "regression_detected") return "✗";
  if (state === "untested") return "○";
  return !stale && ["live_output_observed", "field_observed"].includes(state) ? "●" : "◐";
}
const SESSION_SYMBOLS = {
  idle: ["·", "", "Idle: not running."],
  acquiring: ["◌", "warn", "Acquiring: running, looking for a signal."],
  synchronized: ["◎", "warn", "Synchronized: locked onto the signal's timing, nothing decoded yet."],
  valid_frames: ["●", "ok", "Decoding: frames passed their checksum."],
  unverified_output: ["◉", "ok", "Decoding: output has no checksum to verify it."],
  quiet: ["◇", "", "Quiet: running, nothing heard."],
  unsupported_input: ["⊘", "warn", "Unsupported input: the receiver setup can't feed this decoder (rate, span or mode)."],
  error: ["✗", "bad", "Error: the decoder failed."],
};

export const DECODER_STATUS_LEGEND = {
  installed: "Installed — ✓ ready to use · ⊘ wrong version · ✗ missing",
  tested: "Tested — ○ untested · ◐ partly (synthetic fixtures, a reference recording, or validated on an older msdr version) · ● seen working off-air · ✗ regression. Hover a symbol for the exact level.",
  session: "Now — · idle · ◌ acquiring · ◎ synchronized · ● decoding (checksum ok) · ◉ decoding (unchecked) · ◇ quiet · ⊘ unsupported input · ✗ error",
};

/** {sym, tone, title} for each status column of decoder `d`. `extras` are
 * readinessLabels(d) titles (install hint, validation dates, session detail). */
export function decoderStatusIcons(d, extras = {}) {
  const r = d?.readiness;
  const join = (...parts) => parts.filter(Boolean).join(" ");
  const instState = r?.installation?.state ?? (d?.available ? "present" : "missing");
  const [iSym, iTone, iTitle] = INSTALL_SYMBOLS[instState] ?? INSTALL_SYMBOLS.missing;
  const version = r?.installation?.version && instState === "present" ? `(v${r.installation.version})` : "";
  const testState = r?.validation?.state ?? "untested";
  const stale = !!r?.validation_stale;
  const validation = decoderValidation({ ...d, readiness: { ...r, validation_stale: false } });
  const sessState = r?.session?.state ?? "idle";
  const [sSym, sTone, sTitle] = SESSION_SYMBOLS[sessState] ?? [sessState, "", sessState];
  const counts = [];
  if (r?.session?.events > 0) counts.push(`${r.session.events} events`);
  if (r?.session?.valid_frames > 0) counts.push(`${r.session.valid_frames} valid frames`);
  return {
    installed: { sym: iSym, tone: iTone, title: join(iTitle, version, extras.installed?.title) },
    tested: {
      sym: testSymbol(testState, stale),
      tone: testState === "regression_detected" ? "bad" : "",
      title: join(`${validation.text[0].toUpperCase()}${validation.text.slice(1)}:`, validation.title,
        stale ? "Validated on an older msdr version; worth re-checking on this one." : "",
        extras.tested?.title ? `(${extras.tested.title})` : ""),
    },
    session: { sym: sSym, tone: sTone, title: join(sTitle, counts.length ? `${counts.join(", ")}.` : "", extras.session?.title) },
  };
}
