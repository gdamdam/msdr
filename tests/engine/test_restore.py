import asyncio
import time

import pytest

from msdr.engine.confirmed import ConfirmedState, StateChanged, matches
from msdr.engine.fake_adapter import FakeAdapter
from msdr.engine.restore import (
    ProfileStep,
    RestoreExecutor,
    SetStep,
    plan,
    plan_with_desired,
)
from msdr.engine.state import ProfileSettings, TuneRecipe
from msdr.radio.protocol import PropertyChanged
from msdr.radio.registry import RESTORABLE

CONFIRMED = {
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


def _confirmed_from_adapter(adapter: FakeAdapter) -> ConfirmedState:
    cs = ConfirmedState(api_version=str(adapter.state.get("api_version", "")))
    cs.seed({spec.name: adapter.state[spec.name] for spec in RESTORABLE})
    return cs


# --- plan() -----------------------------------------------------------------


def test_plan_orders_by_stage_and_drops_unchanged():
    profile = ProfileSettings(
        antenna="Antenna B",  # unchanged -- must be dropped
        sample_rate_hz=4_000_000,  # stage 3
        lna_state=5,  # stage 7
        squelch_enabled=True,  # stage 7, after lna_state in registry order
    )
    steps = plan(confirmed=CONFIRMED, profile=profile)
    assert [s.property for s in steps] == ["device_sample_rate", "lna_state", "squelch_enable"]


def test_plan_moves_center_when_vfo_outside_span():
    wide = dict(CONFIRMED, device_sample_rate=2_400_000)
    vfo = wide["device_center_frequency"] + 1_200_000  # outside 0.45 * 2.4 MS/s
    steps = plan(confirmed=wide, recipe=TuneRecipe(frequency_hz=vfo))
    center_step = next(s for s in steps if s.property == "device_center_frequency")
    assert center_step.value == vfo + 10_000

    narrow = dict(CONFIRMED, device_sample_rate=1_000_000)
    vfo2 = narrow["device_center_frequency"] + 600_000  # outside 0.45 * 1 MS/s
    steps2 = plan(confirmed=narrow, recipe=TuneRecipe(frequency_hz=vfo2))
    center_step2 = next(s for s in steps2 if s.property == "device_center_frequency")
    assert center_step2.value == vfo2


def test_plan_keeps_center_when_vfo_inside_span():
    vfo = CONFIRMED["device_center_frequency"] + 50_000  # well inside 0.45 * 2 MS/s
    steps = plan(confirmed=CONFIRMED, recipe=TuneRecipe(frequency_hz=vfo))
    assert not any(s.property == "device_center_frequency" for s in steps)
    assert any(s.property == "device_vfo_frequency" and s.value == vfo for s in steps)


def test_plan_profile_step_first():
    profile = ProfileSettings(native_profile_name="VHF Calling", antenna="Antenna A")
    steps = plan(confirmed=CONFIRMED, profile=profile)
    assert isinstance(steps[0], ProfileStep)
    assert steps[0].name == "VHF Calling"


# --- RestoreExecutor.run() ---------------------------------------------------


async def test_run_confirms_all_steps():
    adapter = FakeAdapter()
    adapter.delay_s = 0.05
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [
        SetStep("lna_state", 5, critical=False, timeout_s=1.0),
        SetStep("squelch_enable", True, critical=False, timeout_s=1.0),
    ]
    progress: list[tuple[str, str]] = []

    async def on_progress(outcome):
        progress.append((outcome.step.property, outcome.status))

    report = await executor.run(steps, on_progress=on_progress)

    assert report.status == "confirmed"
    assert [o.status for o in report.outcomes] == ["confirmed", "confirmed"]
    assert progress == [
        ("lna_state", "pending"),
        ("lna_state", "confirmed"),
        ("squelch_enable", "pending"),
        ("squelch_enable", "confirmed"),
    ]
    assert adapter.state["lna_state"] == 5
    assert adapter.state["squelch_enable"] is True


async def test_run_partial_on_noncritical_failure():
    adapter = FakeAdapter()
    adapter.delay_s = 0.05
    adapter.fail_props = {"lna_state"}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [
        SetStep("lna_state", 5, critical=False, timeout_s=0.2),
        SetStep("squelch_enable", True, critical=False, timeout_s=1.0),
    ]
    report = await executor.run(steps)

    assert report.status == "partial"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[1].status == "confirmed"


async def test_run_failed_on_critical_failure():
    adapter = FakeAdapter()
    adapter.delay_s = 0.05
    adapter.fail_props = {"device_vfo_frequency"}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [
        SetStep("device_vfo_frequency", 146_000_000, critical=True, timeout_s=0.2),
        SetStep("demodulator", "USB", critical=True, timeout_s=1.0),
    ]
    progress: list[tuple[str, str]] = []

    async def on_progress(outcome):
        progress.append((outcome.step.property, outcome.status))

    report = await executor.run(steps, on_progress=on_progress)

    assert report.status == "failed"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[1].status == "skipped"
    # The skipped step is streamed to on_progress too, not just recorded.
    assert progress[-1] == ("demodulator", "skipped")


async def test_run_interrupted_by_external_change():
    adapter = FakeAdapter()
    adapter.delay_s = 0.2
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    async def interfere():
        await asyncio.sleep(0.05)
        await adapter.external_change("device_vfo_frequency", 146_555_000)

    steps = [
        SetStep("lna_state", 5, critical=False, timeout_s=1.0),
        SetStep("squelch_enable", True, critical=False, timeout_s=1.0),
    ]
    asyncio.create_task(interfere())
    report = await executor.run(steps)

    assert report.status == "interrupted"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[1].status == "skipped"


async def test_run_ignores_external_change_on_unregistered_prop():
    adapter = FakeAdapter()
    adapter.delay_s = 0.2
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    async def interfere():
        await asyncio.sleep(0.05)
        # Not in REGISTRY at all -- the real adapter decodes/emits anything
        # SDRconnect reports, including properties msdr doesn't model yet.
        await adapter.external_change("some_new_firmware_prop", "whatever")

    asyncio.create_task(interfere())
    steps = [SetStep("lna_state", 5, critical=False, timeout_s=1.0)]
    report = await executor.run(steps)

    assert report.status == "confirmed"
    assert report.outcomes[0].status == "confirmed"


async def test_run_absorbs_coupled_echo_on_pending_step():
    # F3: SDRconnect couples centre and vfo on real hardware -- setting centre
    # also echoes a vfo change. When that echo lands exactly on what the
    # not-yet-sent vfo step already wants, it must not interrupt the restore,
    # and the vfo step must not send a redundant `set`.
    adapter = FakeAdapter()
    adapter.delay_s = 0.2
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    desired_vfo = CONFIRMED["device_vfo_frequency"] + 210_000

    async def interfere():
        await asyncio.sleep(0.05)
        await adapter.external_change("device_vfo_frequency", desired_vfo)

    steps = [
        SetStep("device_center_frequency", 146_000_000, critical=True, timeout_s=1.0),
        SetStep("device_vfo_frequency", desired_vfo, critical=True, timeout_s=1.0),
    ]
    asyncio.create_task(interfere())
    report = await executor.run(steps)

    assert report.status == "confirmed"
    assert [o.status for o in report.outcomes] == ["confirmed", "confirmed"]
    assert ("set", "device_vfo_frequency", desired_vfo) not in adapter.calls


async def test_run_external_change_on_pending_step_to_other_value_interrupts():
    # Same shape as above, but the echo lands on a value the vfo step does
    # NOT want -- that's real outside interference and must still interrupt.
    adapter = FakeAdapter()
    adapter.delay_s = 0.2
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    desired_vfo = CONFIRMED["device_vfo_frequency"] + 210_000

    async def interfere():
        await asyncio.sleep(0.05)
        await adapter.external_change("device_vfo_frequency", desired_vfo + 5_000)

    steps = [
        SetStep("device_center_frequency", 146_000_000, critical=True, timeout_s=1.0),
        SetStep("device_vfo_frequency", desired_vfo, critical=True, timeout_s=1.0),
    ]
    asyncio.create_task(interfere())
    report = await executor.run(steps)

    assert report.status == "interrupted"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[1].status == "skipped"


async def test_run_get_timeout_after_dropped_echo_fails_step():
    adapter = FakeAdapter()
    adapter.delay_s = 0.05
    adapter.fail_props = {"lna_state"}
    adapter.drop_echo = {"lna_state"}
    adapter.timeout_get = {"lna_state"}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [SetStep("lna_state", 5, critical=False, timeout_s=0.2)]
    report = await executor.run(steps)

    assert report.status == "partial"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].detail == "no response"


async def test_run_profile_refresh_skips_prop_that_times_out():
    adapter = FakeAdapter()
    adapter.profiles["vhf"] = {"active_antenna": "Antenna A"}
    adapter.timeout_get = {"lna_state"}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [ProfileStep(name="vhf", settle_s=0.05, timeout_s=1.0)]
    report = await executor.run(steps)

    assert report.status == "partial"
    assert report.outcomes[0].status == "unverified"
    # lna_state's refresh get() timed out -- run() didn't raise, and confirmed
    # just kept whatever it already had for that prop.
    assert confirmed.props["lna_state"] == CONFIRMED["lna_state"]


async def test_run_profile_step_drains_late_echo_before_next_step():
    adapter = FakeAdapter()
    adapter.delay_s = 0.03  # also slows every adapter.get() during the refresh
    adapter.profiles["vhf"] = {"active_antenna": "Antenna A"}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    async def late_profile_echo():
        # Fires after the 0.02s settle window has already judged us quiet,
        # while the refresh's first (slow) get() is still in flight -- a
        # stray echo of the profile's own change, arriving late.
        await asyncio.sleep(0.04)
        await adapter.external_change("active_antenna", "Antenna C")

    asyncio.create_task(late_profile_echo())
    steps = [
        ProfileStep(name="vhf", settle_s=0.02, timeout_s=2.0),
        SetStep("lna_state", 5, critical=False, timeout_s=1.0),
    ]
    report = await executor.run(steps)

    assert report.status == "partial"
    assert [o.status for o in report.outcomes] == ["unverified", "confirmed"]


async def test_run_reports_failed_when_pump_errors():
    adapter = FakeAdapter()
    adapter.delay_s = 0.02
    confirmed = _confirmed_from_adapter(adapter)
    original_apply = confirmed.apply

    def apply_with_a_bug(ev):
        # Break only for an event unrelated to our own step, so the step's
        # own echo still confirms normally via the pump -- isolating this
        # test to the pump's error handling rather than the (separate,
        # already-guarded) direct apply() call in the step's timeout path.
        if ev.property == "lna_state":
            raise RuntimeError("boom")
        return original_apply(ev)

    confirmed.apply = apply_with_a_bug
    executor = RestoreExecutor(adapter, confirmed)

    async def trigger_bug():
        await asyncio.sleep(0.005)
        await adapter.external_change("lna_state", 9)

    asyncio.create_task(trigger_bug())
    steps = [SetStep("squelch_enable", True, critical=False, timeout_s=0.15)]
    report = await executor.run(steps)

    # The pump's own confirmed.apply() call blew up on an unrelated event --
    # that must surface as a failed report, not an exception out of run() or
    # a silently-lost error.
    assert report.status == "failed"


async def test_run_profile_step_refreshes_state_without_claiming_profile_identity():
    adapter = FakeAdapter()
    adapter.profiles["vhf"] = {"active_antenna": "Antenna A", "lna_state": 1}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [ProfileStep(name="vhf", settle_s=0.05, timeout_s=1.0)]
    report = await executor.run(steps)

    assert report.status == "partial"
    assert report.outcomes[0].status == "unverified"
    assert confirmed.props["active_antenna"] == "Antenna A"
    assert confirmed.props["lna_state"] == 1


async def test_run_profile_step_unverified_when_no_change():
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [ProfileStep(name="unknown_profile", settle_s=0.05, timeout_s=0.2)]
    report = await executor.run(steps)

    assert report.status == "partial"
    assert report.outcomes[0].status == "unverified"
    assert "selection unverified" in report.outcomes[0].detail


async def test_run_confirms_via_get_when_echo_dropped():
    adapter = FakeAdapter()
    adapter.delay_s = 0.05
    adapter.drop_echo = {"lna_state"}
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps = [SetStep("lna_state", 5, critical=False, timeout_s=0.2)]
    report = await executor.run(steps)

    assert report.status == "confirmed"
    assert report.outcomes[0].status == "confirmed"
    assert adapter.state["lna_state"] == 5


async def test_run_concurrent_raises():
    adapter = FakeAdapter()
    adapter.delay_s = 0.2
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)
    steps = [SetStep("lna_state", 5, critical=False, timeout_s=1.0)]

    task = asyncio.create_task(executor.run(steps))
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError):
        await executor.run(steps)
    await task


async def test_run_empty_steps_confirmed():
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    report = await executor.run([])

    assert report.status == "confirmed"
    assert report.outcomes == []
    assert report.duration_s < 0.1


# --- ConfirmedState -----------------------------------------------------------


def test_apply_marks_own_vs_external():
    cs = ConfirmedState()
    cs.seed({"lna_state": 3})

    sc = cs.apply(PropertyChanged("lna_state", 5, "primary", 1.0))
    assert sc == StateChanged("lna_state", 3, 5, "external", 1.0)

    cs.expect("lna_state", 7)
    sc2 = cs.apply(PropertyChanged("lna_state", 7, "primary", 2.0))
    assert sc2 is not None
    assert sc2.cause == "own"
    assert sc2.old == 5
    assert sc2.new == 7

    sc3 = cs.apply(PropertyChanged("lna_state", 7, "primary", 3.0))
    assert sc3 is None

    # An echoed no-op set while an expectation is pending still counts as own.
    cs.expect("lna_state", 7)
    sc4 = cs.apply(PropertyChanged("lna_state", 7, "primary", 4.0))
    assert sc4 is not None
    assert sc4.cause == "own"


def test_readonly_props_do_not_emit():
    cs = ConfirmedState()
    sc = cs.apply(PropertyChanged("signal_power", -70.0, "primary", 1.0))
    assert sc is None
    assert cs.props["signal_power"] == -70.0

    sc2 = cs.apply(PropertyChanged("signal_power", -60.0, "primary", 2.0))
    assert sc2 is None
    assert cs.props["signal_power"] == -60.0


def test_started_changes_emit_but_other_readonly_props_stay_silent():
    cs = ConfirmedState()
    cs.seed({"started": False})
    q = cs.subscribe()

    event = PropertyChanged("started", True, "primary", 1.0)
    changed = cs.apply(event)

    assert changed == StateChanged("started", False, True, "external", 1.0)
    assert q.get_nowait() == changed
    assert cs.apply(PropertyChanged("signal_power", -70.0, "primary", 2.0)) is None
    assert q.empty()


def test_secondary_device_properties_are_ignored():
    cs = ConfirmedState()
    cs.seed({**CONFIRMED, "started": True})
    assert cs.is_complete()
    cs.mark_stale()
    q = cs.subscribe()

    assert cs.apply(PropertyChanged("device_vfo_frequency", 433_920_000, "secondary", 1.0)) is None
    assert cs.apply(PropertyChanged("started", False, "secondary", 2.0)) is None

    assert cs.props["device_vfo_frequency"] == 145_800_000
    assert cs.props["started"] is True
    assert not cs.is_complete()  # a secondary event cannot freshen primary state
    assert q.empty()


def test_matches_does_not_conflate_bool_and_int():
    # squelch_threshold has a 0.5 tolerance; before excluding bool for *both*
    # operands, a value merely close to 1 was wrongly "within tolerance" of
    # a stray True (abs(1.3 - float(True)) <= 0.5).
    assert not matches("squelch_threshold", 1.3, True)
    assert not matches("squelch_threshold", True, 1.3)
    assert matches("squelch_threshold", True, True)


def test_snapshot_requires_complete():
    cs = ConfirmedState()
    with pytest.raises(ValueError):
        cs.snapshot("capture")

    cs.seed(CONFIRMED)
    state = cs.snapshot("capture")
    assert state.vfo_hz == CONFIRMED["device_vfo_frequency"]


# --- desired-state reconciliation around a native profile -------------------


async def test_profile_step_restores_values_the_profile_overwrote():
    """Desired NFM / LNA 3 already match, so the planner emits only the
    ProfileStep -- and the native profile then moves both to AM / LNA 8."""
    adapter = FakeAdapter()
    adapter.profiles["Shortwave"] = {"demodulator": "AM", "lna_state": 8}
    cs = _confirmed_from_adapter(adapter)
    profile = ProfileSettings(native_profile_name="Shortwave", lna_state=3)
    recipe = TuneRecipe(frequency_hz=145_800_000, demodulator="NFM")

    steps, desired = plan_with_desired(confirmed=cs.props, profile=profile, recipe=recipe)
    assert [type(s) for s in steps] == [ProfileStep]
    assert desired["demodulator"] == "NFM"
    assert desired["lna_state"] == 3

    ex = RestoreExecutor(adapter, cs)
    report = await ex.run(steps, desired=desired)

    assert adapter.state["demodulator"] == "NFM"
    assert adapter.state["lna_state"] == 3
    assert report.status == "partial"


async def test_run_reports_partial_when_a_desired_property_ends_up_wrong():
    adapter = FakeAdapter()
    cs = _confirmed_from_adapter(adapter)
    ex = RestoreExecutor(adapter, cs)

    # No steps at all, but the desired state disagrees with confirmed: the
    # final verification has to catch it rather than report "confirmed".
    report = await ex.run([], desired={"demodulator": "AM"})

    assert report.status == "partial"
    assert "demodulator" in report.detail


# --- coupled centre/vfo echoes (RSPdx-R2, F8) -------------------------------


class CoupledCenterAdapter(FakeAdapter):
    """Mimics an RSPdx-R2 under SDRconnect: moving the span makes the receiver
    move the VFO itself, and that echo lands after the centre's own."""

    def __init__(self, coupled_vfo_hz: int, lag_s: float) -> None:
        super().__init__()
        self._coupled_vfo_hz = coupled_vfo_hz
        self._lag_s = lag_s

    async def set(self, prop: str, value: object) -> None:
        await super().set(prop, value)
        if prop == "device_center_frequency":
            self._spawn(self._coupled())

    async def _coupled(self) -> None:
        await asyncio.sleep(self._lag_s)
        await self.external_change("device_vfo_frequency", self._coupled_vfo_hz)


class EchoAfterCoupledAdapter(FakeAdapter):
    """Emits a coupled value for the property being set *before* echoing the
    value we actually asked for."""

    def __init__(self, coupled_vfo_hz: int) -> None:
        super().__init__()
        self._coupled_vfo_hz = coupled_vfo_hz
        self.delay_s = 0.05  # our own echo trails the coupled one

    async def set(self, prop: str, value: object) -> None:
        if prop == "device_vfo_frequency":
            await self.external_change("device_vfo_frequency", self._coupled_vfo_hz)
        await super().set(prop, value)


async def test_coupled_echo_on_the_awaited_property_does_not_interrupt():
    target = 162_550_000
    adapter = EchoAfterCoupledAdapter(coupled_vfo_hz=162_560_000)
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    step = SetStep("device_vfo_frequency", target, critical=True, timeout_s=1.0)
    report = await executor.run([step], desired={"device_vfo_frequency": target})

    assert report.status == "confirmed"
    assert confirmed.props["device_vfo_frequency"] == target


async def test_stable_external_value_on_the_awaited_property_fails_after_timeout():
    target = 162_550_000
    adapter = FakeAdapter()
    adapter.fail_props = {"device_vfo_frequency"}  # our set is silently ignored
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    async def interfere():
        await asyncio.sleep(0.02)
        await adapter.external_change("device_vfo_frequency", 145_900_000)

    asyncio.create_task(interfere())
    step = SetStep("device_vfo_frequency", target, critical=True, timeout_s=0.2)
    started = time.monotonic()
    report = await executor.run([step], desired={"device_vfo_frequency": target})
    elapsed = time.monotonic() - started

    assert elapsed >= 0.2  # waited the whole step timeout rather than aborting
    assert report.status == "failed"
    assert "external change" in report.outcomes[0].detail


async def test_late_coupled_vfo_echo_is_settled_before_the_next_step():
    target = 150_000_000
    adapter = CoupledCenterAdapter(coupled_vfo_hz=target, lag_s=0.05)
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    steps, desired = plan_with_desired(
        confirmed=confirmed.props,
        recipe=TuneRecipe(frequency_hz=target, demodulator="AM"),
    )
    assert [s.property for s in steps] == [
        "device_center_frequency",
        "device_vfo_frequency",
        "demodulator",
    ]
    report = await executor.run(steps, desired=desired)

    # The coupled echo arrives during the centre step's settle window, so it
    # never reaches the demodulator step looking like outside interference --
    # and the vfo step it already satisfied is dropped on the re-plan.
    assert report.status == "confirmed"
    assert ("set", "device_vfo_frequency", target) not in adapter.calls
    assert adapter.state["demodulator"] == "AM"


async def test_am_to_nfm_recovers_speaker_path_and_restores_requested_filter():
    class CoupledModeAdapter(FakeAdapter):
        async def set(self, prop, value):
            await super().set(prop, value)
            if prop == 'demodulator':
                await asyncio.sleep(0.02)
                # SDRconnect applies its mode-default filter after the mode echo.
                await super().set('filter_bandwidth', 180000 if value == 'WFM' else 12500)

    adapter = CoupledModeAdapter()
    adapter.state['demodulator'] = 'AM'
    adapter.state['filter_bandwidth'] = 6000
    cs = _confirmed_from_adapter(adapter)
    freq = adapter.state['device_vfo_frequency']
    steps, desired = plan_with_desired(
        confirmed=cs.props,
        recipe=TuneRecipe(frequency_hz=freq, demodulator='NFM', filter_bandwidth_hz=10000),
    )
    assert [(s.property, s.value) for s in steps][:2] == [
        ('demodulator', 'WFM'), ('demodulator', 'NFM')
    ]
    report = await RestoreExecutor(adapter, cs).run(steps, desired=desired)
    assert report.status == 'confirmed'
    assert adapter.state['demodulator'] == 'NFM'
    assert adapter.state['filter_bandwidth'] == 10000
    assert adapter.state['device_vfo_frequency'] == freq


def test_nfm_recovery_does_not_toggle_an_unchanged_mode():
    steps = plan(confirmed=CONFIRMED, recipe=TuneRecipe(
        frequency_hz=CONFIRMED['device_vfo_frequency'], demodulator='NFM'))
    assert steps == []


async def test_unrelated_change_cannot_confirm_unknown_native_profile():
    adapter = FakeAdapter()
    confirmed = _confirmed_from_adapter(adapter)
    executor = RestoreExecutor(adapter, confirmed)

    async def unrelated_change():
        await asyncio.sleep(0.01)
        await adapter.external_change("lna_state", 4)

    task = asyncio.create_task(unrelated_change())
    report = await executor.run(
        [ProfileStep("nonexistent", settle_s=0.03, timeout_s=0.2)], desired={}
    )
    await task
    assert report.status == "partial"
    assert report.outcomes[0].status == "unverified"
    assert "selection unverified" in report.detail
