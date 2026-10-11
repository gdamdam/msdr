import pytest

from msdr.survey.compare import compare_scans


def scan(id, rows, covered=True):
    return {"id": id, "state": "complete", "config": {"start_hz": 100, "end_hz": 10000},
            "payload": {"results": [{"frequency_hz": hz, "observations": [{"snr_db": snr}]}
                                    for hz, snr in rows],
                        "spans": [{"start_hz": 100, "end_hz": 10000,
                                   "state": "covered" if covered else "uncovered"}]}}


def test_compare_changes_and_one_to_one_matching():
    a = scan("a", [(1000, 10), (5000, 20), (8000, 5)])
    b = scan("b", [(1010, 15), (5010, 21), (6000, 10)])
    result = compare_scans(a, b, 100)
    assert result["new"][0]["frequency_hz"] == 6000
    assert result["disappeared"][0]["frequency_hz"] == 8000
    assert result["changed"][0]["snr_delta_db"] == 5
    assert result["unchanged_count"] == 1
    b["payload"]["results"].append({"frequency_hz": 1020})
    assert len(compare_scans(a, b, 100)["new"]) == 2


def test_uncovered_is_not_disappeared():
    result = compare_scans(scan("a", [(1000, 10)]), scan("b", [], False))
    assert result["disappeared"] == []
    assert len(result["unverified"]) == 1


def test_rejects_different_bands_and_live_sessions():
    a, b = scan("a", []), scan("b", [])
    b["state"] = "running"
    with pytest.raises(ValueError):
        compare_scans(a, b)
    b["state"] = "complete"
    b["config"]["end_hz"] = 20000
    with pytest.raises(ValueError):
        compare_scans(a, b)


def test_center_exclusion_is_not_evidence_of_disappearance():
    before, after = scan("a", [(5000, 10)]), scan("b", [])
    after["payload"]["spans"][0]["usable_intervals_hz"] = [[100, 4900], [5100, 10000]]
    result = compare_scans(before, after)
    assert result["disappeared"] == []
    assert result["unverified"][0]["frequency_hz"] == 5000


def test_compact_comparison_retains_peak_and_identification_history():
    before, after = scan("a", [(5000, 2)]), scan("b", [(5000, 2)])
    before["payload"]["results"][0].update(peak_snr_db=20, identifications=["FM"])
    after["payload"]["results"][0].update(peak_snr_db=30, identifications=["FM"])
    assert compare_scans(before, after)["changed"][0]["snr_delta_db"] == 10
