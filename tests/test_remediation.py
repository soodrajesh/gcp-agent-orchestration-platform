"""The model is untrusted. These tests feed the validator what a wrong or manipulated model might
emit and assert nothing outside the allow-list, or inconsistent with live state, gets through."""

from datetime import UTC, datetime, timedelta

import pytest

from agentorch.cloudrun import FakeRunAdmin, RevisionInfo, ServiceInfo
from agentorch.engine import PermanentStepError
from agentorch.remediation import (
    Rollback,
    execute_rollback,
    parse_diagnosis,
    plan_rollback,
    verify_rollback,
)

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
ALLOWED = frozenset({"ao-demo-target"})


def svc(serving="rev-3", ready3=True, ready2=True):
    revs = (
        RevisionInfo("rev-3", T0 + timedelta(minutes=20), ready3),
        RevisionInfo("rev-2", T0 + timedelta(minutes=10), ready2),
        RevisionInfo("rev-1", T0, True),
    )
    return ServiceInfo("ao-demo-target", revs, {serving: 100})


def rb(service="ao-demo-target", target="rev-2"):
    return Rollback(type="rollback", service=service, target_revision=target)


GOOD = '{"diagnosis":"bad deploy","evidence":["ValueError at handler"],"action":{"type":"rollback","service":"ao-demo-target","target_revision":"rev-2"}}'


# --- parsing -------------------------------------------------------------------------------
def test_parses_plain_json_and_fenced_json_inside_prose():
    assert parse_diagnosis(GOOD).action.target_revision == "rev-2"
    wrapped = f"Here is my analysis.\n```json\n{GOOD}\n```\nHope that helps."
    assert parse_diagnosis(wrapped).diagnosis == "bad deploy"


def test_no_action_is_a_valid_outcome():
    d = parse_diagnosis(
        '{"diagnosis":"transient","evidence":[],"action":{"type":"none","reason":"errors predate deploy"}}'
    )
    assert d.action.type == "none"


@pytest.mark.parametrize(
    "bad",
    [
        "I think you should roll back.",  # no JSON
        '{"diagnosis":"x","action":{"type":"delete_service","service":"ao-demo-target"}}',  # unknown action type
        '{"diagnosis":"x","action":{"type":"rollback","service":"ao-demo-target"}}',  # missing target
        '{"diagnosis":"x"}',  # no action
    ],
)
def test_malformed_or_out_of_schema_proposals_are_rejected(bad):
    with pytest.raises(PermanentStepError):
        parse_diagnosis(bad)


def test_extra_fields_smuggled_into_the_action_are_ignored_not_executed():
    d = parse_diagnosis(
        '{"diagnosis":"x","action":{"type":"rollback","service":"ao-demo-target","target_revision":"rev-2","command":"rm -rf /","percent":0}}'
    )
    assert not hasattr(d.action, "command") and not hasattr(d.action, "percent")


# --- validation against live state ----------------------------------------------------------
async def test_valid_rollback_plans_against_live_state():
    plan = await plan_rollback(rb(), FakeRunAdmin({"ao-demo-target": svc()}), ALLOWED)
    assert (plan.service, plan.current, plan.target) == ("ao-demo-target", "rev-3", "rev-2")


@pytest.mark.parametrize(
    "service", ["mcp-ops", "ao-orchestrator", "../../x", "AO-DEMO", "ao-demo-target; drop"]
)
async def test_non_allowlisted_service_is_refused_and_never_even_read(service):
    admin = FakeRunAdmin({"ao-demo-target": svc()})
    with pytest.raises(PermanentStepError, match="not allow-listed"):
        await plan_rollback(rb(service=service), admin, ALLOWED)
    assert admin.mutations == []


async def test_hallucinated_or_hostile_revision_name_is_refused():
    admin = FakeRunAdmin({"ao-demo-target": svc()})
    for target in ["rev-999", "rev-2; gcloud run services delete x", ""]:
        with pytest.raises(PermanentStepError, match="does not exist"):
            await plan_rollback(rb(target=target), admin, ALLOWED)


async def test_rolling_back_to_the_serving_revision_is_refused():
    with pytest.raises(PermanentStepError, match="already serving"):
        await plan_rollback(rb(target="rev-3"), FakeRunAdmin({"ao-demo-target": svc()}), ALLOWED)


async def test_rolling_forward_is_not_a_rollback():
    with pytest.raises(PermanentStepError, match="not older"):
        await plan_rollback(
            rb(target="rev-3"), FakeRunAdmin({"ao-demo-target": svc(serving="rev-1")}), ALLOWED
        )


async def test_unready_target_is_refused():
    with pytest.raises(PermanentStepError, match="not ready"):
        await plan_rollback(rb(), FakeRunAdmin({"ao-demo-target": svc(ready2=False)}), ALLOWED)


# --- execution ---------------------------------------------------------------------------------
async def test_execute_shifts_traffic_and_verify_confirms():
    admin = FakeRunAdmin({"ao-demo-target": svc()})
    plan = await plan_rollback(rb(), admin, ALLOWED)
    out = await execute_rollback(plan, admin, ALLOWED)
    assert out == {"service": "ao-demo-target", "from": "rev-3", "to": "rev-2"}
    assert admin.mutations == [("ao-demo-target", "rev-2")]
    assert (await verify_rollback(plan, admin))["serving"] == "rev-2"


async def test_state_that_changed_while_awaiting_approval_blocks_execution():
    """A human approves minutes later; if someone else already moved traffic, we must not act on a stale plan."""
    admin = FakeRunAdmin({"ao-demo-target": svc()})
    plan = await plan_rollback(rb(), admin, ALLOWED)
    admin.services["ao-demo-target"] = svc(serving="rev-1")  # someone rolled back further by hand
    with pytest.raises(PermanentStepError, match="changed while awaiting approval"):
        await execute_rollback(plan, admin, ALLOWED)
    assert admin.mutations == []


async def test_verify_fails_loudly_if_traffic_did_not_move():
    admin = FakeRunAdmin({"ao-demo-target": svc()})
    plan = await plan_rollback(rb(), admin, ALLOWED)
    with pytest.raises(PermanentStepError, match="verification failed"):
        await verify_rollback(plan, admin)  # never executed -> still on rev-3


# The exact shape of reply the live model produced: apostrophes copied from a log line and escaped
# as \' (invalid JSON). A correct rollback diagnosis must not be lost to that.
LIVE_REPLY = (
    '{"diagnosis": "errors began with the newest revision", '
    '"evidence": ["\\"message\\": \\"KeyError: \\\'customer_id\\\' in orders.process_checkout\\""], '
    '"action": {"type": "rollback", "service": "ao-demo-target", "target_revision": "rev-2"}}'
)


def test_apostrophe_escape_seen_live_is_repaired_and_the_proposal_still_validates():
    assert "\\'" in LIVE_REPLY
    d = parse_diagnosis(LIVE_REPLY)
    assert d.action.target_revision == "rev-2" and "customer_id" in d.evidence[0]


def test_repair_does_not_relax_the_schema():
    bad = LIVE_REPLY.replace('"rollback"', '"rm_rf"')
    with pytest.raises(PermanentStepError, match="allowed schema"):
        parse_diagnosis(bad)


def test_other_malformed_json_is_still_rejected():
    with pytest.raises(PermanentStepError, match="no JSON"):
        parse_diagnosis(
            '{"diagnosis": "x", "action": {"type": "none",}'
        )  # trailing comma, truncated


async def test_execute_is_idempotent_when_the_change_was_already_applied():
    """At-least-once: an earlier attempt may have applied the traffic shift and still errored."""
    admin = FakeRunAdmin({"ao-demo-target": svc()})
    plan = await plan_rollback(rb(), admin, ALLOWED)
    await execute_rollback(plan, admin, ALLOWED)
    again = await execute_rollback(plan, admin, ALLOWED)  # redelivered step
    assert again["already_applied"] is True
    assert admin.mutations == [("ao-demo-target", "rev-2")]  # still exactly one real mutation
