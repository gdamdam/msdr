import pytest

from msdr.engine.state import Observation, ReceiverState
from msdr.journal.service import JournalService
from msdr.library.models import Signal
from msdr.storage.db import Database
from msdr.storage.repos import JournalRepo, SignalRepo, StateRepo

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
    return ReceiverState.from_props(props, source="observed")


@pytest.fixture
def db():
    database = Database(":memory:")
    database.connect()
    yield database
    database.close()


@pytest.fixture
def service(db):
    return JournalService(JournalRepo(db), SignalRepo(db), StateRepo(db), nearby_hz=5_000)


def test_record_observed_with_no_match_stores_entry_with_signal_id_none(service):
    state = _state()
    entry = service.record_observed(state, Observation(signal_power_db=-70.0, signal_snr_db=10.0))

    assert entry.signal_id is None
    assert entry.kind == "observed"
    assert entry.state_id == state.id
    assert entry.signal_power_db == -70.0
    assert entry.signal_snr_db == 10.0


def test_record_observed_near_known_signal_links_and_touches_heard(service):
    signal = Signal(name="Local repeater", frequency_hz=145_800_000, demodulator="NFM")
    service.signals.add(signal)
    state = _state()

    entry = service.record_observed(state, Observation(signal_power_db=-60.0))

    assert entry.signal_id == signal.id
    updated = service.signals.get(signal.id)
    assert updated.heard_count == 1
    assert updated.first_heard_at is not None
    assert updated.last_heard_at == updated.first_heard_at


def test_record_action_tune_twice_with_same_state_stores_state_once(service):
    state = _state()

    service.record_action("tune", state=state, signal_id="some-signal")
    service.record_action("tune", state=state, signal_id="some-signal")

    row = service.states.db.conn.execute(
        "SELECT COUNT(*) AS n FROM receiver_states WHERE id = ?", (state.id,)
    ).fetchone()
    assert row["n"] == 1
