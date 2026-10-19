import asyncio
from datetime import timedelta

import pytest

from agentorch.engine import (
    Engine,
    LeaseHeldError,
    Step,
    StepContext,
    StepResult,
    StepRetryError,
    Workflow,
)
from agentorch.models import Budget, RunStatus, StepRecord, Usage, utcnow
from agentorch.store import InMemoryRunStore


def make(*steps: Step, worker="w1", store=None):
    store = store or InMemoryRunStore()
    return Engine(store, {"wf": Workflow("wf", tuple(steps))}, worker_id=worker), store


def counter_step(key, calls, usage=None, **kw):
    usage = usage or Usage()

    async def fn(ctx: StepContext) -> StepResult:
        calls[key] = calls.get(key, 0) + 1
        return StepResult(output=f"{key}-out", usage=usage)

    return Step(key, fn, **kw)


async def test_runs_all_steps_and_records_outputs():
    calls = {}
    eng, _ = make(counter_step("a", calls), counter_step("b", calls))
    run = await eng.start("wf", {"x": 1})
    run = await eng.advance(run.id)
    assert run.status is RunStatus.SUCCEEDED
    assert [k for k in run.steps] == ["a", "b"]


async def test_completed_steps_never_rerun_after_failure_and_retry():
    calls, boom = {}, {"left": 1}

    async def flaky(ctx):
        calls["b"] = calls.get("b", 0) + 1
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("transient")
        return StepResult("ok")

    eng, _ = make(counter_step("a", calls, Usage(input_tokens=10)), Step("b", flaky))
    run = await eng.start("wf", {})
    with pytest.raises(StepRetryError):
        await eng.advance(run.id)
    run = await eng.advance(run.id)  # redelivery
    assert run.status is RunStatus.SUCCEEDED
    assert calls == {"a": 1, "b": 2}  # a ran once; only the failing step repeated
    assert run.usage.input_tokens == 10  # and was billed once


async def test_step_fails_permanently_after_max_attempts():
    async def bad(ctx):
        raise ValueError("nope")

    eng, _ = make(Step("a", bad, max_attempts=2))
    run = await eng.start("wf", {})
    with pytest.raises(StepRetryError):
        await eng.advance(run.id)
    run = await eng.advance(run.id)
    assert run.status is RunStatus.FAILED and "ValueError" in run.error


async def test_start_is_idempotent_for_same_run_id():
    eng, _ = make(counter_step("a", {}))
    r1 = await eng.start("wf", {"k": 1}, run_id="alert-42")
    r2 = await eng.start("wf", {"k": 2}, run_id="alert-42")
    assert r2.id == r1.id and r2.input == {"k": 1}


async def test_budget_stops_run_between_steps():
    calls = {}
    eng, _ = make(
        counter_step("a", calls, Usage(input_tokens=600, output_tokens=500)),
        counter_step("b", calls),
    )
    run = await eng.start("wf", {}, budget=Budget(max_tokens=1000, max_cost_usd=1))
    run = await eng.advance(run.id)
    assert run.status is RunStatus.BUDGET_EXCEEDED and "b" not in calls


async def test_cost_budget_enforced():
    calls = {}
    eng, _ = make(counter_step("a", calls, Usage(cost_usd=0.30)), counter_step("b", calls))
    run = await eng.start("wf", {}, budget=Budget(max_cost_usd=0.25))
    assert (await eng.advance(run.id)).status is RunStatus.BUDGET_EXCEEDED


async def test_approval_gate_pauses_then_resumes_on_approve():
    calls = {}
    gated = counter_step(
        "act", calls, approval_prompt=lambda ctx: f"apply fix for {ctx.input['id']}?"
    )
    eng, _ = make(counter_step("plan", calls), gated)
    run = await eng.start("wf", {"id": "INC-1"})
    run = await eng.advance(run.id)
    assert run.status is RunStatus.AWAITING_APPROVAL and "act" not in calls
    assert run.approvals["act"].prompt == "apply fix for INC-1?"
    # advancing again without a decision does nothing and does not re-run plan
    run = await eng.advance(run.id)
    assert run.status is RunStatus.AWAITING_APPROVAL and calls == {"plan": 1}
    await eng.decide(run.id, "act", approved=True, by="rajesh")
    run = await eng.advance(run.id)
    assert run.status is RunStatus.SUCCEEDED and calls == {"plan": 1, "act": 1}


async def test_rejection_ends_run_without_running_gated_step():
    calls = {}
    eng, _ = make(counter_step("act", calls, approval_prompt=lambda c: "ok?"))
    run = await eng.start("wf", {})
    await eng.advance(run.id)
    await eng.decide(run.id, "act", approved=False, by="rajesh")
    run = await eng.advance(run.id)
    assert run.status is RunStatus.REJECTED and calls == {}


async def test_first_approval_decision_wins():
    eng, _ = make(counter_step("act", {}, approval_prompt=lambda c: "ok?"))
    run = await eng.start("wf", {})
    await eng.advance(run.id)
    await eng.decide(run.id, "act", approved=True, by="a")
    run = await eng.decide(run.id, "act", approved=False, by="b")
    assert run.approvals["act"].approved is True and run.approvals["act"].decided_by == "a"


async def test_second_worker_cannot_advance_a_leased_run():
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(ctx):
        started.set()
        await release.wait()
        return StepResult("done")

    store = InMemoryRunStore()
    wf = {"wf": Workflow("wf", (Step("a", slow),))}
    w1, w2 = Engine(store, wf, worker_id="w1"), Engine(store, wf, worker_id="w2")
    run = await w1.start("wf", {})
    t = asyncio.create_task(w1.advance(run.id))
    await started.wait()
    with pytest.raises(LeaseHeldError):
        await w2.advance(run.id)
    release.set()
    assert (await t).status is RunStatus.SUCCEEDED


async def test_expired_lease_can_be_taken_over_and_checkpoints_survive():
    calls = {}
    store = InMemoryRunStore()
    wf = {"wf": Workflow("wf", (counter_step("a", calls), counter_step("b", calls)))}
    w1 = Engine(store, wf, worker_id="w1", lease_seconds=60)
    run = await w1.start("wf", {})
    # simulate w1 crashing after step a: checkpoint a, leave a stale lease behind
    run = await store.get(run.id)
    run.steps["a"] = StepRecord(key="a", output="a-out")
    run.lease_owner, run.lease_expires = "w1", utcnow() - timedelta(seconds=1)
    await store.save(run)
    w2 = Engine(store, wf, worker_id="w2")
    run = await w2.advance(run.id)
    assert run.status is RunStatus.SUCCEEDED and calls == {"b": 1}


async def test_one_engine_instance_excludes_its_own_concurrent_calls():
    """Regression: a single Engine handles many concurrent requests (Cloud Run concurrency > 1),
    so the lease must be per advance() call, not per Engine."""
    started, release, calls = asyncio.Event(), asyncio.Event(), {"n": 0}

    async def slow(ctx):
        calls["n"] += 1
        started.set()
        await release.wait()
        return StepResult("done")

    eng, _ = make(Step("a", slow))
    run = await eng.start("wf", {})
    first = asyncio.create_task(eng.advance(run.id))
    await started.wait()
    with pytest.raises(LeaseHeldError):
        await eng.advance(run.id)  # same Engine, same worker id
    release.set()
    assert (await first).status is RunStatus.SUCCEEDED and calls["n"] == 1


async def test_permanent_error_fails_immediately_without_retry():
    from agentorch.engine import PermanentStepError

    calls = {"n": 0}

    async def invalid(ctx):
        calls["n"] += 1
        raise PermanentStepError("model proposed something we will never run")

    eng, _ = make(Step("a", invalid, max_attempts=5))
    run = await eng.start("wf", {})
    run = await eng.advance(run.id)  # no StepRetryError, no redelivery needed
    assert run.status is RunStatus.FAILED and calls["n"] == 1
    assert "never run" in run.error


async def test_skipped_step_needs_no_approval_and_is_checkpointed():
    calls = {}
    gated = counter_step("act", calls, approval_prompt=lambda c: "ok?", skip_if=lambda c: True)
    eng, _ = make(counter_step("plan", calls), gated, counter_step("after", calls))
    run = await eng.start("wf", {})
    run = await eng.advance(run.id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.steps["act"].output == {"skipped": True} and "act" not in calls
    assert run.approvals == {} and calls == {"plan": 1, "after": 1}


async def test_run_records_step_order_for_viewers():
    eng, _ = make(counter_step("a", {}), counter_step("b", {}))
    assert (await eng.start("wf", {})).step_order == ["a", "b"]


async def test_step_exceptions_are_logged_with_run_and_step(caplog):
    """Found live: a failing step was invisible in the logs and had to be inferred from a 503."""

    async def boom(ctx):
        raise RuntimeError("kaboom")

    eng, _ = make(Step("explode", boom))
    run = await eng.start("wf", {})
    with caplog.at_level("ERROR", logger="agentorch.engine"), pytest.raises(StepRetryError):
        await eng.advance(run.id)
    rec = next(r for r in caplog.records if r.name == "agentorch.engine")
    assert run.id in rec.getMessage() and "explode" in rec.getMessage() and rec.exc_info
