from msdr.engine.history import HistoryStack
from msdr.engine.state import ReceiverState

WIRE_PROPS = {
    "device_vfo_frequency": 145_800_000,
    "device_center_frequency": 145_800_000,
    "device_sample_rate": 2_000_000,
    "demodulator": "NFM",
    "filter_bandwidth": 12_500,
    "lna_state": 3,
    "active_antenna": "Antenna B",
    "squelch_enable": False,
    "squelch_threshold": -100.0,
    "agc_enable": True,
    "agc_threshold": -30.0,
    "noise_reduction_enable": False,
    "noise_reduction_strength": 0,
}


def _state(**overrides) -> ReceiverState:
    props = dict(WIRE_PROPS, **overrides)
    return ReceiverState.from_props(props, source="capture")


def test_push_back_forward_round_trip():
    stack = HistoryStack()
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    stack.push(a)
    stack.push(b)
    assert stack.position == 1
    assert stack.back() is a
    assert stack.position == 0
    assert stack.forward() is b
    assert stack.position == 1


def test_back_and_forward_return_none_at_bounds():
    stack = HistoryStack()
    assert stack.back() is None
    assert stack.forward() is None
    stack.push(_state())
    assert stack.back() is None  # only one entry, already at it
    assert stack.forward() is None


def test_push_after_back_truncates_forward_entries():
    stack = HistoryStack()
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    c = _state(device_vfo_frequency=147_000_000)
    stack.push(a)
    stack.push(b)
    stack.back()
    stack.push(c)
    assert stack.entries == [a, c]
    assert stack.position == 1
    assert not stack.can_forward()


def test_duplicate_consecutive_push_collapses():
    stack = HistoryStack()
    a = _state()
    a_again = _state()  # equal to_props(), different id/captured_at
    stack.push(a)
    stack.push(a_again)
    assert stack.entries == [a]
    assert stack.position == 0


def test_duplicate_push_after_back_truncates_forward_entries():
    stack = HistoryStack()
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    stack.push(a)
    stack.push(b)
    stack.back()
    stack.push(_state())  # duplicate of a -- collapses, but must still drop b

    assert stack.entries == [a]
    assert not stack.can_forward()


def test_max_entries_trims_oldest():
    stack = HistoryStack(max_entries=2)
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    c = _state(device_vfo_frequency=147_000_000)
    stack.push(a)
    stack.push(b)
    stack.push(c)
    assert stack.entries == [b, c]
    assert stack.position == 1


def test_dump_load_round_trip():
    stack = HistoryStack(max_entries=50)
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    stack.push(a)
    stack.push(b)
    stack.back()

    dumped = stack.dump()
    assert dumped == {"position": 0, "state_ids": [a.id, b.id]}

    restored = HistoryStack.load(stack.entries, dumped["position"], max_entries=50)
    assert restored.entries == [a, b]
    assert restored.position == 0
    assert restored.can_forward()


def test_load_clamps_out_of_range_position():
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    stack = HistoryStack.load([a, b], position=5)  # stale/corrupt persisted cursor

    assert stack.position == 1
    assert stack.back() is a
    assert stack.forward() is b


def test_load_clamps_negative_position_on_empty_states():
    stack = HistoryStack.load([], position=3)
    assert stack.position == -1
    assert stack.back() is None
    assert stack.forward() is None


def test_load_trims_to_max_entries():
    states = [_state(device_vfo_frequency=145_800_000 + i * 1_000) for i in range(5)]
    stack = HistoryStack.load(states, position=4, max_entries=3)

    assert len(stack.entries) <= 3
    assert stack.entries == states[2:]
    assert stack.entries[stack.position] is states[4]
    assert stack.back() is states[3]
    assert stack.forward() is states[4]


def test_undo_move_reverts_back_and_forward():
    stack = HistoryStack()
    a = _state()
    b = _state(device_vfo_frequency=146_000_000)
    stack.push(a)
    stack.push(b)

    stack.back()
    assert stack.position == 0
    stack.undo_move("back")
    assert stack.position == 1

    stack.forward()  # already at end, no-op (position stays 1)
    stack.undo_move("forward")
    assert stack.position == 0


def test_undo_move_clamps_within_bounds():
    stack = HistoryStack()
    stack.push(_state())
    stack.undo_move("forward")  # would go to -1, clamp keeps it valid range
    assert stack.position == -1
    stack.undo_move("forward")
    assert stack.position == -1  # already at the floor

    stack2 = HistoryStack()
    stack2.push(_state())
    stack2.undo_move("back")  # would go past the end, clamp at last index
    assert stack2.position == 0
