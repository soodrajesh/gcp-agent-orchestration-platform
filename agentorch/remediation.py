"""Turning a model's proposal into an action we are willing to execute.

The model is untrusted: it reads log text that an attacker may influence. So its output is only a
*proposal*, parsed into a closed schema and then checked against live state by code. The only action
that exists is "roll traffic back to an older, ready revision of an allow-listed service". A
proposal for anything else, any other service, or a revision that is not what the model claimed,
fails the run instead of reaching the approval gate."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from .cloudrun import RunAdmin, ServiceInfo
from .engine import PermanentStepError

SERVICE_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


class Rollback(BaseModel):
    type: Literal["rollback"]
    service: str
    target_revision: str


class NoAction(BaseModel):
    type: Literal["none"]
    reason: str = ""


class Diagnosis(BaseModel):
    diagnosis: str
    evidence: list[str] = Field(default_factory=list)
    action: Annotated[Rollback | NoAction, Field(discriminator="type")]


def _repair(text: str) -> str:
    """Models copy log text containing apostrophes and escape them as \\', which JSON forbids
    (seen live: a correct rollback diagnosis was rejected because of it). That is the only repair:
    anything else malformed is still rejected, and the schema below is not relaxed at all."""
    return text.replace("\\'", "'")


def parse_diagnosis(text: str) -> Diagnosis:
    """Pull the first JSON object out of the model's reply (it may wrap it in prose or a fence)."""
    decoder = json.JSONDecoder()
    for candidate in (text, _repair(text)):
        for m in re.finditer(r"\{", candidate):
            try:
                obj, _ = decoder.raw_decode(candidate[m.start() :])
            except ValueError:
                continue
            if isinstance(obj, dict) and "action" in obj:
                try:
                    return Diagnosis.model_validate(obj)
                except ValidationError as exc:
                    raise PermanentStepError(
                        f"model proposal does not match the allowed schema: {exc.errors()[0]['msg']}"
                    ) from exc
    raise PermanentStepError("model reply contained no JSON proposal")


@dataclass(frozen=True)
class RollbackPlan:
    service: str
    current: str
    target: str


def check_rollback(action: Rollback, info: ServiceInfo, allowed: frozenset[str]) -> RollbackPlan:
    """Pure check of a proposal against a snapshot of the service. Raises PermanentStepError."""
    if not SERVICE_RE.match(action.service) or action.service not in allowed:
        raise PermanentStepError(f"service {action.service!r} is not allow-listed for rollback")
    current = info.serving
    if current is None:
        raise PermanentStepError(f"{action.service} has no serving revision")
    revs = {r.name: r for r in info.revisions}
    target = revs.get(action.target_revision)
    if target is None:
        raise PermanentStepError(
            f"revision {action.target_revision!r} does not exist on {action.service}"
        )
    if action.target_revision == current:
        raise PermanentStepError("target revision is already serving")
    if not target.ready:
        raise PermanentStepError(f"revision {action.target_revision} is not ready")
    if current in revs and target.created >= revs[current].created:
        raise PermanentStepError(
            "target revision is not older than the serving revision: not a rollback"
        )
    return RollbackPlan(action.service, current, action.target_revision)


async def plan_rollback(action: Rollback, admin: RunAdmin, allowed: frozenset[str]) -> RollbackPlan:
    # Allow-list first: do not even read a service we would never touch.
    if action.service not in allowed:
        raise PermanentStepError(f"service {action.service!r} is not allow-listed for rollback")
    return check_rollback(action, await admin.get(action.service), allowed)


async def execute_rollback(plan: RollbackPlan, admin: RunAdmin, allowed: frozenset[str]) -> dict:
    """Re-validates against *current* state: a human may have approved minutes after the plan."""
    if plan.service not in allowed:
        raise PermanentStepError(f"service {plan.service!r} is not allow-listed for rollback")
    info = await admin.get(plan.service)
    if info.traffic.get(plan.target) == 100:
        # Idempotent: a previous attempt (or a person) already did exactly this. This step is
        # at-least-once, and the first attempt may have applied the change but still errored.
        return {
            "service": plan.service,
            "from": plan.current,
            "to": plan.target,
            "already_applied": True,
        }
    if info.serving != plan.current:
        raise PermanentStepError(
            f"{plan.service} changed while awaiting approval (was serving {plan.current}, now {info.serving}); not acting"
        )
    check_rollback(
        Rollback(type="rollback", service=plan.service, target_revision=plan.target), info, allowed
    )
    await admin.set_traffic(plan.service, plan.target)
    return {"service": plan.service, "from": plan.current, "to": plan.target}


async def verify_rollback(plan: RollbackPlan, admin: RunAdmin) -> dict:
    info = await admin.get(plan.service)
    if info.traffic.get(plan.target) != 100:
        raise PermanentStepError(
            f"verification failed: traffic is {info.traffic}, expected 100% on {plan.target}"
        )
    return {"service": plan.service, "serving": plan.target, "traffic": info.traffic}
