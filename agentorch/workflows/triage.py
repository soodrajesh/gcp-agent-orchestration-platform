"""Incident triage: diagnose (A2A specialist reading real logs) -> plan (code validates the model's
proposal against live state) -> rollback (human-approved, re-validated at execution) -> verify.

Only the first step involves a model. Everything that touches infrastructure is deterministic."""

from __future__ import annotations

from ..a2a_client import ask_agent
from ..cloudrun import RunAdmin
from ..engine import Step, StepContext, StepResult, Workflow
from ..models import Usage
from ..remediation import (
    Rollback,
    RollbackPlan,
    execute_rollback,
    parse_diagnosis,
    plan_rollback,
    verify_rollback,
)


def _plan_of(ctx: StepContext) -> RollbackPlan:
    p = ctx.output_of("plan")["rollback"]
    return RollbackPlan(p["service"], p["current"], p["target"])


def _no_action(ctx: StepContext) -> bool:
    return ctx.output_of("plan")["rollback"] is None


def build_triage(
    specialist_url: str, *, authenticate: bool, model: str, admin: RunAdmin, allowed: frozenset[str]
) -> Workflow:
    async def diagnose(ctx: StepContext) -> StepResult:
        return await ask_agent(
            specialist_url,
            f"Alert: {ctx.input['alert']}",
            name="incident_analyst",
            model=model,
            authenticate=authenticate,
        )

    async def plan(ctx: StepContext) -> StepResult:
        d = parse_diagnosis(ctx.output_of("diagnose"))
        rollback = None
        if isinstance(d.action, Rollback):
            p = await plan_rollback(d.action, admin, allowed)
            rollback = {"service": p.service, "current": p.current, "target": p.target}
        return StepResult(
            {
                "diagnosis": d.diagnosis,
                "evidence": d.evidence,
                "action": d.action.model_dump(),
                "rollback": rollback,
            }
        )

    def prompt(ctx: StepContext) -> str:
        out, p = ctx.output_of("plan"), _plan_of(ctx)
        evidence = "\n".join(f"  - {e}" for e in out["evidence"][:3]) or "  (none cited)"
        return (
            f"Roll back {p.service}: send 100% of traffic from {p.current} to {p.target}.\n\n"
            f"Diagnosis: {out['diagnosis']}\n\nEvidence cited by the analyst:\n{evidence}"
        )

    async def rollback(ctx: StepContext) -> StepResult:
        return StepResult(await execute_rollback(_plan_of(ctx), admin, allowed), Usage())

    async def verify(ctx: StepContext) -> StepResult:
        return StepResult(await verify_rollback(_plan_of(ctx), admin), Usage())

    return Workflow(
        "triage",
        (
            Step("diagnose", diagnose),
            Step("plan", plan),
            Step("rollback", rollback, approval_prompt=prompt, skip_if=_no_action),
            Step("verify", verify, skip_if=_no_action),
        ),
    )
