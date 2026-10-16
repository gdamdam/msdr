"""SensingAccumulator on synthetic power-code series."""

from __future__ import annotations

import numpy as np

from msdr.wifi.sensing import SensingAccumulator

SRC = "aa:bb:cc:dd:ee:ff"
T0 = 1000.0


def _feed(acc, ts, vals, src=SRC):
    for t, v in zip(ts, vals, strict=True):
        acc.add(float(t), src, v)


def _flat(rng, secs, fps=15):
    ts = T0 + np.arange(int(secs * fps)) / fps
    return ts, 110 + rng.normal(0, 0.5, len(ts))


def test_flat_series_no_presence():
    rng = np.random.default_rng(1)
    acc = SensingAccumulator(SRC)
    ts, v = _flat(rng, 90)
    _feed(acc, ts, v)
    assert acc.presence(ts[-1])["motion"] is False


def test_noise_burst_triggers_presence():
    rng = np.random.default_rng(2)
    acc = SensingAccumulator(SRC)
    ts, v = _flat(rng, 90)
    _feed(acc, ts, v)
    start = ts[-1] + 1 / 15
    bt = start + np.arange(45) / 15
    seen = None
    for t in bt:
        acc.add(float(t), SRC, 110 + rng.normal(0, 6))
        if seen is None and acc.presence(t)["motion"]:
            seen = t
    assert seen is not None and seen - start <= 2.0


def test_one_second_dip_counts_once():
    rng = np.random.default_rng(3)
    acc = SensingAccumulator(SRC)
    ts, v = _flat(rng, 45)
    v[30 * 15 : 31 * 15] -= 10
    _feed(acc, ts, v)
    assert acc.crossings == 1
    last = acc.crossing(ts[-1])["last_ts"]
    assert T0 + 30 <= last <= T0 + 31


def _sine(rng, fps, secs=40):
    ts = np.sort(T0 + np.arange(int(secs * fps)) / fps + rng.uniform(0, 0.4 / fps, int(secs * fps)))
    return ts, 110 + 3 * np.sin(2 * np.pi * 0.3 * ts) + rng.normal(0, 0.5, len(ts))


def test_breathing_rate_dense():
    rng = np.random.default_rng(4)
    acc = SensingAccumulator(SRC)
    ts, v = _sine(rng, 100)
    _feed(acc, ts, v)
    b = acc.breathing(ts[-1])
    assert b is not None and abs(b["bpm"] - 18) < 1
    assert acc.snapshot(ts[-1])["breathing_status"] == "ok"


def test_breathing_needs_dense_source():
    rng = np.random.default_rng(5)
    acc = SensingAccumulator(SRC)
    ts, v = _sine(rng, 10)
    _feed(acc, ts, v)
    assert acc.breathing(ts[-1]) is None
    assert acc.snapshot(ts[-1])["breathing_status"] == "needs dense source"


def test_other_addresses_dropped():
    acc = SensingAccumulator(SRC)
    for i in range(1000):
        acc.add(T0 + i * 0.01, "11:22:33:44:55:66", 100)
    acc.add(T0 + 20, SRC.upper(), None)
    assert acc.rows() == []
    assert acc.fps(T0 + 10) == 0.0


def test_breathing_none_across_a_gap_and_clock_step_back_is_dropped():
    acc = SensingAccumulator("aa:bb:cc:dd:ee:01")
    src = "aa:bb:cc:dd:ee:01"
    t = 1000.0
    for i in range(1500):                       # 15 s at 100/s, a 0.3 Hz rhythm
        t = 1000.0 + i / 100
        acc.add(t, src, int(110 + 3 * np.sin(2 * np.pi * 0.3 * t)))
    for i in range(1500):                       # then 5 s of silence, then 15 s more
        t2 = 1020.0 + i / 100
        acc.add(t2, src, int(110 + 3 * np.sin(2 * np.pi * 0.3 * t2)))
    assert acc.breathing(1035.0) is None
    n = len(acc._samples)
    acc.add(1000.0, src, 110)                   # clock stepped back: dropped
    assert len(acc._samples) == n
