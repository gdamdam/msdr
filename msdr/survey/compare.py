"""Compare observed signals only where both sessions covered the same band."""


def compare_scans(before: dict, after: dict, tolerance_hz: float = 2000) -> dict:
    if before["id"] == after["id"]:
        raise ValueError("choose two different sessions")
    if any(before["config"].get(k) != after["config"].get(k)
           for k in ("start_hz", "end_hz")):
        raise ValueError("sessions must cover the same band")
    if before["state"] in {"running", "paused"} or after["state"] in {"running", "paused"}:
        raise ValueError("finish both sessions before comparing")

    def covered(scan, hz):
        for span in scan.get("payload", {}).get("spans", []):
            # New reports record the usable intervals explicitly; a visited
            # centre or an excluded DC bin is not evidence of absence.
            if "usable_intervals_hz" in span:
                if any(low <= hz < high for low, high in span["usable_intervals_hz"]):
                    return True
            elif span.get("state") == "covered" and span["start_hz"] <= hz <= span["end_hz"]:
                return True  # Legacy coverage had no per-bin measurement data.
        return False

    def summary(row):
        obs = row.get("observations", [])
        snrs = [o["snr_db"] for o in obs if isinstance(o.get("snr_db"), (float, int))]
        names = sorted({str((o.get("confirmed") or {}).get("name") or
                           (o.get("family") or {}).get("name") or "") for o in obs})
        return {"frequency_hz": row["frequency_hz"],
                "peak_snr_db": row.get("peak_snr_db", max(snrs) if snrs else None),
                "identifications": row.get("identifications", names)}

    old = [summary(r) for r in before.get("payload", {}).get("results", [])]
    new = [summary(r) for r in after.get("payload", {}).get("results", [])]
    # Globally closest pairs avoid input order changing which nearby signal matches.
    pairs = sorted((abs(a["frequency_hz"] - b["frequency_hz"]), i, j)
                   for i, a in enumerate(old) for j, b in enumerate(new)
                   if abs(a["frequency_hz"] - b["frequency_hz"]) <= tolerance_hz)
    used_old, used_new, changes = set(), set(), []
    unchanged = 0
    for _, i, j in pairs:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        a, b = old[i], new[j]
        delta = (b["peak_snr_db"] - a["peak_snr_db"]
                 if a["peak_snr_db"] is not None and b["peak_snr_db"] is not None else None)
        if a["identifications"] != b["identifications"] or (delta is not None and abs(delta) >= 3):
            changes.append({"before": a, "after": b, "snr_delta_db": delta})
        else:
            unchanged += 1
    missing = [a for i, a in enumerate(old) if i not in used_old]
    added = [b for j, b in enumerate(new) if j not in used_new]
    return {"before_id": before["id"], "after_id": after["id"],
            "tolerance_hz": tolerance_hz, "snr_change_threshold_db": 3,
            "new": [b for b in added if covered(before, b["frequency_hz"])],
            "disappeared": [a for a in missing if covered(after, a["frequency_hz"])],
            "unverified": [b for b in added if not covered(before, b["frequency_hz"])] +
                          [a for a in missing if not covered(after, a["frequency_hz"])],
            "changed": changes, "unchanged_count": unchanged}
