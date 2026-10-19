"""Checkpointed workflow engine.

Guarantees (each covered by tests/test_engine.py):
  * A step that finished is recorded in the store and is never executed again, so a crash,
    redelivered Pub/Sub message or second worker cannot re-bill a completed LLM call.
  * Only the step that was *in flight* when a worker died can run twice (at-least-once for that
    one step), so keep step bodies idempotent or cheap to repeat.
  * Two workers cannot advance the same run: a lease is taken with compare-and-set on `version`.
  * Spend is checked between steps against the run's token/cost budget.
  * A step with an approval prompt pauses the run until a human decides.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from opentelemetry import trace  # no-op unless a tracer provider is configured (obs.setup_tracing)

from .guard import NullGuard, SpendGuard
from .models import Approval, Budget, Run, RunStatus, StepRecord, Usage, utcnow
from .store import ConflictError, NotFoundError, RunStore

log = logging.getLogger("agentorch.engine")
tracer = trace.get_tracer("agentorch.engine")


class LeaseHeldError(RuntimeError):
    """Another worker owns this run right now; the dispatcher should retry later."""


class PermanentStepError(RuntimeError):
    """Raise from a step when retrying cannot help (e.g. invalid input); fails the run at once."""


class StepRetryError(RuntimeError):
    """A step failed but has attempts left; the dispatcher should redeliver (nack)."""


@dataclass(frozen=True)
class StepResult:
    output: Any
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class StepContext:
    run: Run

    @property
    def input(self) -> dict[str, Any]:
        return self.run.input

    def output_of(self, key: str) -> Any:
        return self.run.steps[key].output


StepFn = Callable[[StepContext], Awaitable[StepResult]]


@dataclass(frozen=True)
class Step:
    key: str
    fn: StepFn
    approval_prompt: Callable[[StepContext], str] | None = None
    max_attempts: int = 3
    # Decided from earlier outputs only. A skipped step needs no approval and is checkpointed.
    skip_if: Callable[[StepContext], bool] | None = None


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: tuple[Step, ...]

    def __post_init__(self) -> None:
        keys = [s.key for s in self.steps]
        if len(set(keys)) != len(keys):
            raise ValueError(f"workflow {self.name}: duplicate step keys {keys}")


class Engine:
    def __init__(
        self,
        store: RunStore,
        workflows: dict[str, Workflow],
        *,
        worker_id: str | None = None,
        lease_seconds: float = 300,
        guard: SpendGuard | None = None,
    ) -> None:
        self._store = store
        self._workflows = workflows
        self._worker = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._lease = timedelta(seconds=lease_seconds)
        self.guard: SpendGuard = guard or NullGuard()

    async def start(
        self,
        workflow: str,
        input: dict[str, Any],
        *,
        run_id: str | None = None,
        budget: Budget | None = None,
    ) -> Run:
        """Create a run. Idempotent for a caller-supplied `run_id` (e.g. an alert's dedupe key)."""
        if workflow not in self._workflows:
            raise KeyError(f"unknown workflow {workflow!r}")
        run = Run(
            id=run_id or uuid.uuid4().hex,
            workflow=workflow,
            input=input,
            budget=budget or Budget(),
            step_order=[st.key for st in self._workflows[workflow].steps],
        )
        try:
            return await self._store.create(run)
        except ConflictError:
            return await self._store.get(run.id)

    async def advance(self, run_id: str) -> Run:
        run = await self._store.get(run_id)
        if run.status.terminal:
            return run
        if run.status is RunStatus.AWAITING_APPROVAL and not self._pending_decision_made(run):
            return run

        # Lease owner is unique per call, not per Engine: one Engine serves many concurrent
        # requests on a Cloud Run instance, and they must exclude each other too.
        owner = f"{self._worker}/{uuid.uuid4().hex[:8]}"
        run = await self._claim(run, owner)
        wf = self._workflows[run.workflow]

        for i, step in enumerate(wf.steps):
            if step.key in run.steps:
                continue  # checkpointed: never re-run

            if self._over_budget(run):
                return await self._finish(run, RunStatus.BUDGET_EXCEEDED, "budget exhausted")

            if step.skip_if is not None and step.skip_if(StepContext(run)):
                run.steps[step.key] = StepRecord(key=step.key, output={"skipped": True})
                run = await self._save_or_lose_lease(run, step.key)
                continue

            if step.approval_prompt is not None:
                approval = run.approvals.get(step.key)
                if approval is None:
                    run.approvals[step.key] = Approval(
                        step_key=step.key, prompt=step.approval_prompt(StepContext(run))
                    )
                    return await self._release(run, RunStatus.AWAITING_APPROVAL)
                if not approval.decided:
                    return await self._release(run, RunStatus.AWAITING_APPROVAL)
                if not approval.approved:
                    return await self._finish(run, RunStatus.REJECTED, f"{step.key} rejected")

            if (reason := await self.guard.halt_reason()) is not None:
                return await self._finish(run, RunStatus.HALTED, reason)

            heartbeat = asyncio.create_task(self._heartbeat(run.id, owner))
            try:
                with tracer.start_as_current_span(
                    f"step {step.key}",
                    attributes={"run.id": run.id, "step.key": step.key, "workflow": run.workflow},
                ):
                    result = await step.fn(StepContext(run))
            except PermanentStepError as exc:
                return await self._finish(run, RunStatus.FAILED, f"{step.key}: {exc}")
            except Exception as exc:
                run.attempts[step.key] = run.attempts.get(step.key, 0) + 1
                log.exception(
                    "step failed run=%s step=%s attempt=%d/%d",
                    run.id,
                    step.key,
                    run.attempts[step.key],
                    step.max_attempts,
                )
                if run.attempts[step.key] >= step.max_attempts:
                    return await self._finish(
                        run, RunStatus.FAILED, f"{step.key}: {type(exc).__name__}: {exc}"
                    )
                await self._release(run, RunStatus.PENDING)
                raise StepRetryError(f"{step.key} failed ({exc}); will retry") from exc
            finally:
                heartbeat.cancel()

            run.steps[step.key] = StepRecord(key=step.key, output=result.output, usage=result.usage)
            run.usage = run.usage + result.usage
            run = await self._save_or_lose_lease(run, step.key)
            await self.guard.record(result.usage)
            log.info(  # the per-step cost ledger row (routed to BigQuery by a log sink)
                "step completed",
                extra={
                    "event": "step_usage",
                    "run_id": run.id,
                    "workflow": run.workflow,
                    "step": step.key,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cost_usd": result.usage.cost_usd,
                },
            )

            if i < len(wf.steps) - 1 and self._over_budget(run):
                return await self._finish(run, RunStatus.BUDGET_EXCEEDED, "budget exhausted")

        return await self._finish(run, RunStatus.SUCCEEDED, None)

    async def decide(self, run_id: str, step_key: str, *, approved: bool, by: str) -> Run:
        for _ in range(5):
            run = await self._store.get(run_id)
            approval = run.approvals.get(step_key)
            if approval is None:
                raise NotFoundError(f"no approval requested for {step_key}")
            if approval.decided:
                return run  # idempotent: first decision wins
            approval.decided, approval.approved = True, approved
            approval.decided_by, approval.decided_at = by, utcnow()
            run.status = RunStatus.PENDING
            try:
                return await self._store.save(run)
            except ConflictError:
                continue
        raise ConflictError(f"could not record decision for {run_id}")

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _pending_decision_made(run: Run) -> bool:
        return any(a.decided for a in run.approvals.values())

    @staticmethod
    def _over_budget(run: Run) -> bool:
        return (
            run.usage.total_tokens >= run.budget.max_tokens
            or run.usage.cost_usd >= run.budget.max_cost_usd
        )

    async def _heartbeat(self, run_id: str, owner: str) -> None:
        """Keep the lease alive while a (possibly slow) step runs, so a long LLM call cannot be
        mistaken for a crashed worker and run twice. Stops quietly if the lease is lost."""
        while True:
            await asyncio.sleep(self._lease.total_seconds() / 3)
            try:
                if not await self._store.extend_lease(run_id, owner, utcnow() + self._lease):
                    return
            except Exception:
                log.warning("lease heartbeat failed run=%s", run_id, exc_info=True)

    async def _save_or_lose_lease(self, run: Run, step_key: str) -> Run:
        if run.lease_owner is not None:
            run.lease_expires = utcnow() + self._lease  # refresh at every step boundary
        try:
            return await self._store.save(run)
        except ConflictError as exc:
            raise LeaseHeldError(f"lost lease on {run.id} during {step_key}") from exc

    async def _claim(self, run: Run, owner: str) -> Run:
        now = utcnow()
        if run.lease_owner is not None and run.lease_expires and run.lease_expires > now:
            raise LeaseHeldError(f"{run.id} is leased by {run.lease_owner}")
        run.lease_owner, run.lease_expires = owner, now + self._lease
        run.status = RunStatus.RUNNING
        try:
            return await self._store.save(run)
        except ConflictError as exc:
            raise LeaseHeldError(f"{run.id} was claimed concurrently") from exc

    async def _release(self, run: Run, status: RunStatus) -> Run:
        run.lease_owner, run.lease_expires, run.status = None, None, status
        return await self._store.save(run)

    async def _finish(self, run: Run, status: RunStatus, error: str | None) -> Run:
        run.error = error
        return await self._release(run, status)
