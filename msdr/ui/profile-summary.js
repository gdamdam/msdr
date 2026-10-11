// Compact one-line summary of a receiver profile's stored (non-null) settings.
// Pure so the library list and tests share it.
export function profileSummary(p) {
  const on = (v) => (v ? "on" : "off");
  const parts = [];
  // SDRconnect names antennas "Antenna B"; only prefix bare ids like "B".
  if (p.antenna != null) parts.push(/^antenna\b/i.test(p.antenna) ? p.antenna : `Antenna ${p.antenna}`);
  if (p.lna_state != null) parts.push(`LNA ${p.lna_state}`);
  if (p.sample_rate_hz != null) parts.push(`${+(p.sample_rate_hz / 1e6).toFixed(3)} MS/s`);
  if (p.audio_agc_enabled != null) parts.push(`AGC ${on(p.audio_agc_enabled)}`);
  if (p.noise_reduction_enabled != null) parts.push(`NR ${on(p.noise_reduction_enabled)}`);
  if (p.squelch_enabled != null) parts.push(`Squelch ${on(p.squelch_enabled)}`);
  if (p.native_profile_name != null) parts.push(`Native ${p.native_profile_name}`);
  return parts.join(" · ");
}

// Map confirmed wire props to ProfileSettings fields (registry.py PropertySpec
// wire_name -> field_name) so "Save current settings as profile" prefills the
// editor. Absent props stay null (= "don't change" in a profile).
export function profileFromProps(props) {
  const p = props || {};
  const pick = (k) => (p[k] === undefined ? null : p[k]);
  return {
    antenna: pick("active_antenna"),
    lna_state: pick("lna_state"),
    sample_rate_hz: pick("device_sample_rate"),
    audio_agc_enabled: pick("agc_enable"),
    audio_agc_threshold: pick("agc_threshold"),
    noise_reduction_enabled: pick("noise_reduction_enable"),
    noise_reduction_strength: pick("noise_reduction_strength"),
    squelch_enabled: pick("squelch_enable"),
    squelch_threshold: pick("squelch_threshold"),
  };
}
