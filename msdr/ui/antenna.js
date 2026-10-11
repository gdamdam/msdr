// Antenna select helpers shared by instrument-panel.js's receiver ANT select
// and drawer.js's saved-signal Antenna field, so both read "Antenna B" as
// "B" and show an in-flight request the same way.

/** "Antenna A,Antenna B,Antenna C" -> [{value:"Antenna A", letter:"A"}, ...].
 * Returns [] for an empty/missing string -- callers decide their own
 * fallback (the receiver panel disables rather than guessing; the library
 * edit form falls back to a fixed A/B/C list). */
export function antennaOptions(valid) {
  return (typeof valid === "string" ? valid : "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((value) => ({ value, letter: value.replace(/^Antenna\s+/i, "") || value }));
}

const letterOf = (value) => {
  if (!value) return null;
  const m = String(value).match(/^Antenna\s+([ABC])$/i);
  return m ? m[1].toUpperCase() : String(value);
};

/** The ANT readout's text: the confirmed value's letter normally, or the
 * requested value's letter with a pending marker ("B…") while a set-antenna
 * request hasn't echoed back yet in props.active_antenna. Never shows the
 * requested value as if it were confirmed -- callers must keep using
 * `confirmed` (props.active_antenna) once a request settles, pending or not. */
export function antennaDisplay(confirmed, pending) {
  if (pending) return `${letterOf(pending) ?? pending}…`;
  return letterOf(confirmed) ?? "--";
}
