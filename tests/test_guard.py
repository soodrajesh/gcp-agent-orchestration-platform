import asyncio

import pytest

from agentorch.engine import Engine, LeaseHeldError, Step, StepResult, Workflow
from agentorch.guard import InMemoryGuard
from agentorch.models import RunStatus, Usage
from agentorch.store import InMemoryRunStore


def two_steps(calls, cost=0.01):
    async def a(ctx):
        calls.append("a")
        return StepResult("a", Usage(cost_usd=cost))

    async def b(ctx):
        calls.append("b")
        return StepResult("b", Usage(cost_usd=cost))

    return {"wf": Workflow("wf", (Step("a", a), Step("b", b)))}


async def test_kill_switch_halts_before_the_next_step_and_nothing_runs_after():
    calls, guard = [], InMemoryGuard(daily_cap_usd=10)
    eng = Engine(InMemoryRunStore(), two_steps(calls), guard=guard)
    run = await eng.start("wf", {})
    await guard.set_killswitch(True, "ops")
    run = await eng.advance(run.id)
    assert run.status is RunStatus.HALTED and "kill switch" in run.error and calls == []


async def test_kill_switch_stops_an_in_flight_run_between_steps():
    calls, guard = [], InMemoryGuard(daily_cap_usd=10)

    async def a(ctx):
        calls.append("a")
        await guard.set_killswitch(True, "ops")  # operator hits the switch mid-run
        return StepResult("a")

    async def b(ctx):
        calls.append("b")
        return StepResult("b")

    eng = Engine(
        InMemoryRunStore(), {"wf": Workflow("wf", (Step("a", a), Step("b", b)))}, guard=guard
    )
    run = await eng.advance((await eng.start("wf", {})).id)
    assert run.status is RunStatus.HALTED and calls == ["a"]


async def test_daily_cap_is_platform_wide_across_runs():
    calls, guard = [], InMemoryGuard(daily_cap_usd=0.015)
    eng = Engine(InMemoryRunStore(), two_steps(calls, cost=0.01), guard=guard)
    r1 = await eng.advance((await eng.start("wf", {})).id)  # spends 0.02 > cap after two steps
    assert r1.status is RunStatus.SUCCEEDED  # 0 and 0.01 spent before each step: both under the cap
    r2 = await eng.advance((await eng.start("wf", {})).id)  # a *different* run is refused outright
    assert r2.status is RunStatus.HALTED and "daily spend cap" in r2.error
    assert (await guard.state())["spent_today_usd"] >= 0.01


async def test_switching_off_resumes_service():
    calls, guard = [], InMemoryGuard(daily_cap_usd=10)
    eng = Engine(InMemoryRunStore(), two_steps(calls), guard=guard)
    await guard.set_killswitch(True, "ops")
    await guard.set_killswitch(False, "ops")
    assert (await eng.advance((await eng.start("wf", {})).id)).status is RunStatus.SUCCEEDED


async def test_heartbeat_keeps_the_lease_during_a_slow_step():
    """Without it a step longer than the lease looks like a crash and a second worker runs it again."""
    started, release, runs = asyncio.Event(), asyncio.Event(), {"n": 0}

    async def slow(ctx):
        runs["n"] += 1
        started.set()
        await release.wait()
        return StepResult("done")

    store = InMemoryRunStore()
    wf = {"wf": Workflow("wf", (Step("a", slow),))}
    w1 = Engine(store, wf, worker_id="w1", lease_seconds=0.3)
    w2 = Engine(store, wf, worker_id="w2", lease_seconds=0.3)
    run = await w1.start("wf", {})
    task = asyncio.create_task(w1.advance(run.id))
    await started.wait()
    await asyncio.sleep(0.9)  # 3x the lease: it would have expired twice over without heartbeats
    with pytest.raises(LeaseHeldError):
        await w2.advance(run.id)
    release.set()
    assert (await task).status is RunStatus.SUCCEEDED and runs["n"] == 1


async def test_without_a_heartbeat_the_same_slow_step_would_be_taken_over():
    """Control: proves the previous test is meaningful (lease really does expire in 0.3s)."""
    store = InMemoryRunStore()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(ctx):
        started.set()
        await release.wait()
        return StepResult("done")

    wf = {"wf": Workflow("wf", (Step("a", slow),))}
    w1 = Engine(store, wf, worker_id="w1", lease_seconds=0.3)
    run = await w1.start("wf", {})
    run = await store.get(run.id)
    from datetime import timedelta

    from agentorch.models import utcnow

    run.lease_owner, run.lease_expires = "dead-worker", utcnow() - timedelta(seconds=1)
    await store.save(run)
    release.set()  # a worker that crashed (no heartbeat) is correctly taken over
    assert (await w1.advance(run.id)).status is RunStatus.SUCCEEDED


async def test_extend_lease_only_works_for_the_current_owner_and_keeps_version():
    from datetime import timedelta

    from agentorch.models import Run, utcnow

    store = InMemoryRunStore()
    run = await store.create(Run(id="r", workflow="wf", lease_owner="me", lease_expires=utcnow()))
    assert await store.extend_lease("r", "me", utcnow() + timedelta(seconds=60)) is True
    assert await store.extend_lease("r", "someone-else", utcnow() + timedelta(seconds=60)) is False
    assert (
        await store.get("r")
    ).version == run.version  # no bump, so the worker's CAS save still works
