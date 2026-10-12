"""Reconciling restore: diff a desired state against confirmed into
an ordered set of steps, then drive an adapter through them, confirming each
step against a ConfirmedState so a restore recovers from timeouts, dropped
echoes and outside interference (someone touching SDRconnect's own GUI).
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from msdr.engine.confirmed import ConfirmedState, StateChanged, matches
from msdr.engine.state import ProfileSettings, ReceiverState, TuneRecipe
from msdr.radio.protocol import PropertyChanged, RadioAdapter
from msdr.radio.registry import REGISTRY, RESTORABLE

# Moving the span makes SDRconnect move the VFO itself (observed on an
# RSPdx-R2), and that coupled echo can land well after the centre frequency's
# own. Let it arrive and be absorbed before judging the next step, rather than
# having it show up mid-step and look like someone turning the dial.
_CENTER_SETTLE_S = 0.3
_MODE_SETTLE_S = 0.2


@dataclass(frozen=True)
class SetStep:
    property: str
    value: object
    critical: bool
    timeout_s: float


@dataclass(frozen=True)
class ProfileStep:
    name: str
    settle_s: float = 0.4
    timeout_s: float = 5.0


Step = SetStep | ProfileStep


@dataclass
class StepOutcome:
    step: Step
    status: Literal["pending", "confirmed", "unverified", "failed", "skipped"]
    detail: str = ""


@dataclass
class RestoreReport:
    status: Literal["confirmed", "partial", "interrupted", "failed"]
    outcomes: list[StepOutcome]
    duration_s: float
    detail: str = ""


def desired_props(
    *,
    confirmed: dict[str, object],
    profile: ProfileSettings | None = None,
    recipe: TuneRecipe | None = None,
    full: ReceiverState | None = None,
) -> dict[str, object]:
    """Every property this restore wants, *including* those already matching
    `confirmed`.

    Kept unpruned because a native ProfileStep can move properties the planner
    would otherwise drop as "nothing to do": the executor re-plans against this
    dict once the profile has landed, and verifies it at the end.
    """
    if full is not None:
        # Centre is whatever `full` says -- no recomputation.
        desired: dict[str, object] = full.to_props()
    else:
        desired = {}
        if profile is not None:
            desired.update(profile.to_props())
        if recipe is not None:
            desired["device_vfo_frequency"] = recipe.frequency_hz
            if recipe.demodulator is not None:
                desired["demodulator"] = recipe.demodulator
            if recipe.filter_bandwidth_hz is not None:
                desired["filter_bandwidth"] = recipe.filter_bandwidth_hz

        # Recentre only when a vfo move is actually desired, and only far
        # enough off-centre that the current span wouldn't hold it.
        if "device_vfo_frequency" in desired:
            sr = desired.get("device_sample_rate", confirmed.get("device_sample_rate"))
            center = desired.get(
                "device_center_frequency", confirmed.get("device_center_frequency")
            )
            vfo = desired["device_vfo_frequency"]
            if sr is not None and center is not None:
                span = 0.45 * sr
                if vfo < center - span or vfo > center + span:
                    desired["device_center_frequency"] = vfo + 10_000 if sr > 2_000_000 else vfo

    return desired


def _set_steps(desired: dict[str, object], confirmed: dict[str, object]) -> list[SetStep]:
    """The SetSteps still outstanding for `desired` given `confirmed`, ordered
    by stage (RESTORABLE is already sorted; ties keep registry order)."""
    steps: list[SetStep] = []
    for spec in RESTORABLE:
        if spec.name not in desired:
            continue
        if spec.name in confirmed and matches(spec.name, confirmed[spec.name], desired[spec.name]):
            continue
        if (spec.name == "demodulator" and desired[spec.name] == "NFM"
                and confirmed.get(spec.name) == "AM"):
            # SDRconnect 1.0.10 can leave local speaker playback silent after
            # AM -> NFM despite valid PCM/mute state. The operator-confirmed
            # WFM -> NFM transition rebuilds that path without retuning.
            steps.append(SetStep("demodulator", "WFM", True, spec.timeout_s))
        steps.append(
            SetStep(
                property=spec.name,
                value=desired[spec.name],
                critical=spec.critical,
                timeout_s=spec.timeout_s,
            )
        )
    return steps


def plan_with_desired(
    *,
    confirmed: dict[str, object],
    profile: ProfileSettings | None = None,
    recipe: TuneRecipe | None = None,
    full: ReceiverState | None = None,
) -> tuple[list[Step], dict[str, object]]:
    """The planned steps plus the unpruned desired state they came from.

    Callers that drive RestoreExecutor should use this and hand `desired` to
    run(), so a native profile switch can't quietly undo a property the
    planner had pruned.
    """
    desired = desired_props(confirmed=confirmed, profile=profile, recipe=recipe, full=full)
    steps: list[Step] = []
    if profile is not None and profile.native_profile_name:
        steps.append(ProfileStep(name=profile.native_profile_name))
    steps.extend(_set_steps(desired, confirmed))
    return steps, desired


def plan(
    *,
    confirmed: dict[str, object],
    profile: ProfileSettings | None = None,
    recipe: TuneRecipe | None = None,
    full: ReceiverState | None = None,
) -> list[Step]:
    steps, _desired = plan_with_desired(
        confirmed=confirmed, profile=profile, recipe=recipe, full=full
    )
    return steps


class RestoreExecutor:
    def __init__(
        self,
        adapter: RadioAdapter,
        confirmed: ConfirmedState,
        *,
        own_subscription: bool = True,
    ) -> None:
        self._adapter = adapter
        self._confirmed = confirmed
        self._own_subscription = own_subscription
        self._running = False
        self._pump_error: Exception | None = None

    @property
    def running(self) -> bool:
        return self._running

    def feed(self, ev: PropertyChanged) -> StateChanged | None:
        """Drive confirmed state from an adapter event seen elsewhere (Task 8,
        when own_subscription=False so events aren't applied twice)."""
        return self._confirmed.apply(ev)

    async def run(
        self,
        steps: list[Step],
        on_progress: Callable[[StepOutcome], Awaitable[None]] | None = None,
        desired: dict[str, object] | None = None,
    ) -> RestoreReport:
        if self._running:
            raise RuntimeError("a restore is already running")
        self._running = True
        self._pump_error = None
        start = time.monotonic()
        steps = list(steps)  # re-planned in place after a ProfileStep; don't mutate the caller's
        outcomes: list[StepOutcome] = []
        q = self._confirmed.subscribe()
        adapter_q = self._adapter.subscribe() if self._own_subscription else None
        pump_task = asyncio.create_task(self._pump(adapter_q)) if adapter_q is not None else None
        stop_reason: Literal["interrupted", "failed"] | None = None
        try:
            idx = -1
            while idx + 1 < len(steps):  # index-driven: a ProfileStep re-plans the tail
                idx += 1
                step = steps[idx]
                if stop_reason is None and self._pump_error is not None:
                    stop_reason = "failed"

                if stop_reason is not None:
                    outcome = StepOutcome(step, "skipped")
                    outcomes.append(outcome)
                    if on_progress is not None:
                        await on_progress(outcome)
                    continue

                outcome = StepOutcome(step, "pending")
                outcomes.append(outcome)
                if on_progress is not None:
                    await on_progress(outcome)

                try:
                    if isinstance(step, ProfileStep):
                        result, detail = await self._run_profile_step(step, q)
                    else:
                        # Desired values for steps this plan hasn't sent yet --
                        # an external echo on one of these (e.g. SDRconnect's
                        # centre/vfo coupling, F3) isn't interference as long
                        # as it already lands on what that step wants.
                        pending = {
                            s.property: s.value
                            for s in steps[idx + 1 :]
                            if isinstance(s, SetStep)
                        }
                        result, detail = await self._run_set_step(step, q, pending)
                except ConnectionError:
                    result, detail = "failed", "connection error"
                    stop_reason = "failed"
                else:
                    if result == "interrupted":
                        stop_reason = "interrupted"
                    elif result == "failed" and isinstance(step, SetStep) and step.critical:
                        stop_reason = "failed"

                outcome.status = result if result in {"confirmed", "unverified"} else "failed"
                outcome.detail = detail
                if on_progress is not None:
                    await on_progress(outcome)

                if (
                    isinstance(step, SetStep)
                    and step.property == "device_center_frequency"
                    and result == "confirmed"
                    and desired is not None
                ):
                    # Only safe with `desired`: draining means giving up the
                    # chance to call an echo interference, so we must be able
                    # to re-check what's left against where the radio actually
                    # is and put back anything the coupling moved.
                    await self._settle(q, _CENTER_SETTLE_S)
                    steps[idx + 1 :] = _set_steps(desired, self._confirmed.props)

                if (
                    isinstance(step, SetStep)
                    and step.property == "demodulator"
                    and result == "confirmed"
                    and desired is not None
                ):
                    # Mode changes restore SDRconnect's per-mode filter after
                    # the mode echo. Absorb it, then re-plan any explicit BW.
                    await self._settle(q, _MODE_SETTLE_S)
                    steps[idx + 1 :] = _set_steps(desired, self._confirmed.props)

                if (isinstance(step, ProfileStep) and result in {"confirmed", "unverified"}
                        and desired is not None):
                    # The profile just rewrote the receiver wholesale, possibly
                    # over properties the planner had pruned as "already
                    # matching". Re-plan the remaining sets from the full
                    # desired state against the freshly refreshed confirmed
                    # state rather than trusting the pre-profile diff.
                    steps[idx + 1 :] = _set_steps(desired, self._confirmed.props)

            if stop_reason is None and self._pump_error is not None:
                stop_reason = "failed"

            mismatches: list[str] = []
            if desired is not None and stop_reason is None:
                # Last word on the whole restore: every desired property, not
                # just the ones that got a step.
                for prop, value in desired.items():
                    actual = self._confirmed.props.get(prop)
                    if not matches(prop, actual, value):
                        mismatches.append(f"{prop} expected {value!r}, got {actual!r}")

            if stop_reason == "interrupted":
                status: Literal["confirmed", "partial", "interrupted", "failed"] = "interrupted"
            elif stop_reason == "failed":
                status = "failed"
            elif any(o.status in {"failed", "unverified"} for o in outcomes) or mismatches:
                status = "partial"
            else:
                status = "confirmed"
            duration_s = time.monotonic() - start
            return RestoreReport(
                status=status,
                outcomes=outcomes,
                duration_s=duration_s,
                detail="; ".join(mismatches + [
                    o.detail for o in outcomes if o.status == "unverified"
                ]),
            )
        finally:
            self._confirmed.clear_expectations()
            self._confirmed.unsubscribe(q)
            if pump_task is not None:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
            if adapter_q is not None:
                self._adapter.unsubscribe(adapter_q)
            self._running = False

    async def _pump(self, adapter_q: asyncio.Queue) -> None:
        try:
            while True:
                ev = await adapter_q.get()
                if isinstance(ev, PropertyChanged):
                    self._confirmed.apply(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # recorded below, not swallowed
            # Surface this as a failed report instead of letting it escape
            # from the `await pump_task` in run()'s finally and mask whatever
            # RestoreReport was otherwise about to be returned.
            self._pump_error = exc

    async def _run_set_step(
        self, step: SetStep, q: asyncio.Queue, pending: dict[str, object] | None = None
    ) -> tuple[str, str]:
        pending = pending or {}

        # A still-pending step's property may already have been pushed to the
        # desired value by an echo absorbed while an earlier step was being
        # awaited (see the `pending` handling below) -- nothing to send (F3).
        if matches(step.property, self._confirmed.props.get(step.property), step.value):
            return "confirmed", "already matched"

        self._confirmed.expect(step.property, step.value)
        await self._adapter.set(step.property, step.value)

        saw_external = False
        deadline = time.monotonic() + step.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                sc: StateChanged = await asyncio.wait_for(q.get(), remaining)
            except TimeoutError:
                break
            if sc.property == step.property and sc.cause == "own":
                return "confirmed", ""
            if sc.cause == "external":
                spec = REGISTRY.get(sc.property)
                if sc.property == step.property:
                    # A non-matching echo of the very property we're setting:
                    # SDRconnect emits an intermediate/coupled value before the
                    # echo of our own set. Aborting here reported "interrupted"
                    # for a tune that then landed exactly right, so keep
                    # waiting -- the timeout below is the real verdict.
                    saw_external = True
                    continue
                if spec is not None and spec.stage > 0:
                    if sc.property in pending and matches(
                        sc.property, sc.new, pending[sc.property]
                    ):
                        # Absorbed: a not-yet-sent step already wants this
                        # value (coupled centre/vfo on real hardware, F3).
                        # confirmed.props already reflects it (apply() always
                        # updates); that step's own top-of-function check
                        # will see the match and skip sending.
                        continue
                    return "interrupted", f"external change on {sc.property}"
                # External change on an unrestorable/unregistered property --
                # SDRconnect emits property_changed for anything, so this
                # isn't outside interference with the restore. Keep waiting.
            # else: unrelated own confirmation (e.g. left over from a
            # profile refresh) -- keep waiting for ours.

        # No echo within the timeout (possibly dropped) -- ask directly.
        try:
            value = await self._adapter.get(step.property)
        except TimeoutError:
            return "failed", "no response"
        self._confirmed.apply(PropertyChanged(step.property, value, "primary", time.monotonic()))
        if matches(step.property, value, step.value):
            return "confirmed", ""
        if saw_external:
            return "failed", (
                f"external change on {step.property}: expected {step.value!r}, got {value!r}"
            )
        return "failed", f"expected {step.value!r}, got {value!r}"

    async def _settle(self, q: asyncio.Queue, seconds: float) -> None:
        """Absorb (already-applied) echoes the device couples to the change we
        just made, so the next step doesn't mistake them for interference."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(q.get(), remaining)
            except TimeoutError:
                return

    async def _run_profile_step(self, step: ProfileStep, q: asyncio.Queue) -> tuple[str, str]:
        await self._adapter.apply_profile(step.name)

        deadline = time.monotonic() + step.timeout_s
        while True:
            remaining_total = deadline - time.monotonic()
            if remaining_total <= 0:
                break
            wait_time = min(step.settle_s, remaining_total)
            try:
                await asyncio.wait_for(q.get(), wait_time)
            except TimeoutError:
                break

        # Refresh confirmed state from the device so it's authoritative after
        # a profile switch. Expectations are set first so these synthetic
        # events count as "own" and don't look like outside interference to
        # whatever step runs next.
        for spec in RESTORABLE:
            try:
                value = await self._adapter.get(spec.name)
            except TimeoutError:
                # Leave this one as whatever confirmed already had; a single
                # slow prop shouldn't fail the whole profile switch.
                continue
            self._confirmed.expect(spec.name, value)
            self._confirmed.apply(PropertyChanged(spec.name, value, "primary", time.monotonic()))

        # Any echo of the profile's own changes that arrives late (after the
        # settle window judged us quiet, e.g. while the refresh above was
        # still awaiting a slow get()) is queued but stale by now -- discard
        # it here so it can't be mistaken for outside interference by the
        # next step.
        self._drain(q)
        # No profile identity/acknowledgment is exposed by the adapter.
        # Property changes could also be from the operator or another client.
        return "unverified", (
            "native profile selection unverified; SDRconnect provides no acknowledgment"
        )

    def _drain(self, q: asyncio.Queue) -> None:
        while not q.empty():
            q.get_nowait()
