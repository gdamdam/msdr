from msdr.survey.scan_spectrum import SpectrumCapture


def frame(center=256, at=1):
    bins = [-100.0] * 512
    bins[100] = -20.0
    bins[256] = 100.0  # DC artifact must never contaminate stitched peak levels.
    return {"bins": bins, "center_hz": center, "span_hz": 512, "at": at, "center_exclusion_bins": 2}


def test_native_mapping_center_hole_and_overlapping_capture():
    capture = SpectrumCapture(0, 512, native_bin_width_hz=1)
    intervals = capture.add(frame())
    assert intervals == [[0, 254.0], [259.0, 512]]
    result = capture.export()
    assert result["holes"] == [
        {"start_hz": 254.0, "end_hz": 259.0, "reason": "uncaptured or center-excluded spectrum"}
    ]
    strongest = max(result["bins"], key=lambda item: item["max_db"])
    assert strongest["frequency_hz"] == 100
    assert strongest["max_db"] == -20
    slice_ = result["slices"][0]
    assert slice_["first_bin_hz"] == 0
    assert slice_["bin_width_hz"] * len(slice_["values_db"]) == 512
    assert slice_["values_db"][254:259] == [None] * 5
    capture.add(frame(center=300, at=2))
    assert capture.export()["holes"] == []


def test_time_history_is_bounded_and_retains_first_and_last_band():
    capture = SpectrumCapture(0, 20000, native_bin_width_hz=1, max_slices=16)
    for i in range(1000):
        capture.add(frame(center=256 + i * 10, at=i + 1))
    result = capture.export()
    assert len(result["bins"]) <= 8192
    assert len(result["slices"]) <= 16
    assert result["slices"][0]["at"] == 1
    assert result["slices"][-1]["at"] == 1000
    assert result["slice_interval_s"] > 1
    assert result["time_history_sampled"]
    assert len(result["holes"]) >= 1


def test_no_center_exclusion_retains_center_bin_and_full_coverage():
    capture = SpectrumCapture(0, 512, native_bin_width_hz=1)
    spectrum = frame()
    spectrum["center_exclusion_bins"] = -1
    assert capture.add(spectrum) == [[0, 512]]
    exported = capture.export()
    assert exported["holes"] == []
    assert exported["slices"][0]["values_db"][256] == 100.0
    assert max(exported["bins"], key=lambda item: item["max_db"])["frequency_hz"] == 256


def rolled_off(center, at=1):
    # Flat -100 dB floor whose outer eighth on each side sags 10 dB, like a
    # receiver filter's roll-off.
    bins = [-110.0 if i < 64 or i >= 448 else -100.0 for i in range(512)]
    return {"bins": bins, "center_hz": center, "span_hz": 512, "at": at}


def test_overlap_takes_each_bin_from_the_slice_nearer_its_centre():
    capture = SpectrumCapture(0, 896, native_bin_width_hz=1)
    # 25% overlap: [0, 512) and [384, 896) share 384-512, which is the
    # second slice's sagging left edge and the first slice's flat inside.
    capture.add(rolled_off(256, at=1))
    capture.add(rolled_off(640, at=2))
    capture.add(rolled_off(256, at=3))  # a revisit still counts towards the owner
    bins = {round(b["frequency_hz"]): b for b in capture.export()["bins"]}
    for hz in (384, 420, 447):  # overlap owned by the first slice: flat there
        assert bins[hz]["typical_db"] == -100.0
        assert bins[hz]["sample_count"] == 2
    for hz in (448, 500):  # nearer the second slice's centre, which is flat there
        assert bins[hz]["typical_db"] == -100.0
    assert bins[10]["typical_db"] == -110.0  # an outer edge has no better view
