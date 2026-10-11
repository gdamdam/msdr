/** Pass times use the observer timezone returned with predictions. */
export function passTime(iso, timezone) {
  if (!iso || !Number.isFinite(Date.parse(iso))) return "—";
  return new Date(iso).toLocaleTimeString([], { timeZone: timezone || undefined, hour: "2-digit", minute: "2-digit", hour12: false });
}
export function passDate(iso, timezone) {
  if (!iso || !Number.isFinite(Date.parse(iso))) return "—";
  return new Date(iso).toLocaleDateString([], { timeZone: timezone || undefined, weekday: "short", month: "short", day: "numeric" });
}
