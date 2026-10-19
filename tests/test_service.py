"""Orchestrator API end to end, in one event loop: HTTP -> dispatcher -> engine -> a *real* A2A
specialist server (scripted model) -> validation -> approval gate -> rollback on a fake Cloud Run."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from starlette.requests import Request

from agentorch.auth import Caller, Unauthenticated
from agentorch.cloudrun import FakeRunAdmin, RevisionInfo, ServiceInfo
from agentorch.dispatch import LocalDispatcher
from agentorch.engine import Engine
from agentorch.models import Run, RunStatus
from agentorch.service import Settings, create_app
from agentorch.store import InMemoryRunStore
from agentorch.workflows.triage import build_triage

PUSH_SA = "pusher@proj.iam.gserviceaccount.com"
OPERATOR = "rajesh@example.com"
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def target_service():
    revs = (
        RevisionInfo("rev-3", T0 + timedelta(minutes=20), True),
        RevisionInfo("rev-2", T0 + timedelta(minutes=10), True),
    )
    return ServiceInfo("ao-demo-target", revs, {"rev-3": 100})


class HeaderAuth:
    """Test double: identity comes from an X-User header, like a verified token would give."""

    async def __call__(self, request: Request) -> Caller:
        email = request.headers.get("x-user")
        if not email:
            raise Unauthenticated("no identity")
        return Caller(email)


def build(port, store, admin, guard=None):
    wf = build_triage(
        f"http://127.0.0.1:{port}",
        authenticate=False,
        model="fake",
        admin=admin,
        allowed=frozenset({"ao-demo-target"}),
    )
    engine = Engine(store, {"triage": wf}, guard=guard)
    dispatcher = LocalDispatcher(engine)
    app = create_app(
        engine,
        store,
        dispatcher,
        HeaderAuth(),
        Settings(frozenset({OPERATOR}), PUSH_SA),
        {"triage"},
    )
    return app, dispatcher


@pytest.fixture
async def system(specialist_server):
    port, specialist_llm = specialist_server
    store, admin = InMemoryRunStore(), FakeRunAdmin({"ao-demo-target": target_service()})
    app, dispatcher = build(port, store, admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        yield c, dispatcher, store, specialist_llm, admin


def op(**extra):
    return {"x-user": OPERATOR, **extra}


async def start(c, alert="Errors on ao-demo-target", **kw):
    r = await c.post(
        "/runs", headers=op(), json={"workflow": "triage", "input": {"alert": alert}, **kw}
    )
    assert r.status_code == 202
    return r.json()["id"]


async def get(c, rid):
    return (await c.get(f"/runs/{rid}", headers=op())).json()


async def test_full_flow_diagnose_plan_approve_rollback_verify(system):
    c, dispatcher, _, llm, admin = system
    rid = await start(c, "Spend and errors up on ao-demo-target")
    await dispatcher.drain()

    run = await get(c, rid)
    assert run["status"] == "awaiting_approval"
    assert run["step_order"] == ["diagnose", "plan", "rollback", "verify"]
    assert list(run["steps"]) == ["diagnose", "plan"]
    assert run["steps"]["plan"]["output"]["rollback"] == {
        "service": "ao-demo-target",
        "current": "rev-3",
        "target": "rev-2",
    }
    assert "from rev-3 to rev-2" in run["approvals"]["rollback"]["prompt"].replace(
        "traffic from", "from"
    )
    assert (
        "customer_id" in run["approvals"]["rollback"]["prompt"]
    )  # the evidence is shown to the human
    assert "Spend and errors" in llm.seen[0]  # the alert reached the remote agent
    assert run["usage"]["input_tokens"] > 0  # usage crossed the A2A boundary into the budget
    assert admin.mutations == []  # NOTHING has changed infrastructure before approval

    r = await c.post(f"/runs/{rid}/approvals/rollback", headers=op(), json={"approved": True})
    assert r.status_code == 200
    await dispatcher.drain()
    run = await get(c, rid)
    assert run["status"] == "succeeded"
    assert admin.mutations == [("ao-demo-target", "rev-2")]
    assert run["steps"]["verify"]["output"]["serving"] == "rev-2"
    assert run["approvals"]["rollback"]["decided_by"] == OPERATOR
    assert len(llm.seen) == 1  # the model was consulted exactly once across the pause


async def test_rejection_is_terminal_and_changes_nothing(system):
    c, dispatcher, _, _, admin = system
    rid = await start(c)
    await dispatcher.drain()
    await c.post(f"/runs/{rid}/approvals/rollback", headers=op(), json={"approved": False})
    await dispatcher.drain()
    assert (await get(c, rid))["status"] == "rejected"
    assert admin.mutations == []


@pytest.mark.parametrize(
    "proposal",
    [
        # prompt-injected / hallucinated proposals: each must fail BEFORE any approval is requested
        '{"diagnosis":"x","action":{"type":"rollback","service":"ao-orchestrator","target_revision":"rev-2"}}',
        '{"diagnosis":"x","action":{"type":"rollback","service":"ao-demo-target","target_revision":"rev-404"}}',
        '{"diagnosis":"x","action":{"type":"delete_service","service":"ao-demo-target"}}',
        "Sure! I have rolled it back for you.",
    ],
)
async def test_bad_model_proposals_fail_the_run_before_any_human_prompt(system, proposal):
    c, dispatcher, _, llm, admin = system
    llm.replies[:] = [proposal]
    rid = await start(c)
    await dispatcher.drain()
    run = await get(c, rid)
    assert run["status"] == "failed" and run["approvals"] == {}
    assert run["error"].startswith("plan:")
    assert admin.mutations == []


async def test_no_action_diagnosis_finishes_without_asking_a_human(system):
    c, dispatcher, _, llm, admin = system
    llm.replies[:] = [
        '{"diagnosis":"errors predate the deploy","evidence":[],"action":{"type":"none","reason":"not caused by release"}}'
    ]
    rid = await start(c)
    await dispatcher.drain()
    run = await get(c, rid)
    assert run["status"] == "succeeded" and run["approvals"] == {}
    assert run["steps"]["rollback"]["output"] == {"skipped": True}
    assert admin.mutations == []


async def test_traffic_that_moved_elsewhere_during_approval_blocks_the_rollback(system):
    """Someone shipped a newer revision while the approval was pending: the plan is stale, do not act."""
    c, dispatcher, _, _, admin = system
    rid = await start(c)
    await dispatcher.drain()
    revs = (RevisionInfo("rev-4", T0 + timedelta(minutes=40), True), *target_service().revisions)
    admin.services["ao-demo-target"] = ServiceInfo("ao-demo-target", revs, {"rev-4": 100})
    await c.post(f"/runs/{rid}/approvals/rollback", headers=op(), json={"approved": True})
    await dispatcher.drain()
    run = await get(c, rid)
    assert run["status"] == "failed" and "changed while awaiting approval" in run["error"]
    assert admin.mutations == []


async def test_unauthenticated_and_non_operator_are_refused(system):
    c, *_ = system
    mallory = {"x-user": "mallory@evil.test"}
    assert (await c.get("/runs")).status_code == 401
    assert (await c.get("/runs", headers=mallory)).status_code == 403
    assert (await c.post("/runs", headers=mallory, json={"workflow": "triage"})).status_code == 403
    assert (
        await c.post("/runs/x/approvals/rollback", headers=mallory, json={"approved": True})
    ).status_code == 403


async def test_validation(system):
    c, *_ = system
    assert (await c.post("/runs", headers=op(), json={"workflow": "nope"})).status_code == 400
    assert (await c.get("/runs/missing", headers=op())).status_code == 404
    rid = await start(c, run_id="dup")
    r = await c.post(f"/runs/{rid}/approvals/rollback", headers=op(), json={"approved": "yes"})
    assert r.status_code == 400


async def test_start_is_idempotent_on_run_id(system):
    c, dispatcher, *_ = system
    for _ in range(2):
        await start(c, run_id="alert-7")
    await dispatcher.drain()
    assert len((await c.get("/runs", headers=op())).json()) == 1


def envelope(payload):
    return {"message": {"data": base64.b64encode(json.dumps(payload).encode()).decode()}}


async def test_push_endpoint_only_accepts_the_push_service_account(system):
    c, *_ = system
    assert (
        await c.post("/internal/advance", headers=op(), json=envelope({"run_id": "x"}))
    ).status_code == 403
    assert (await c.post("/internal/advance", json=envelope({"run_id": "x"}))).status_code == 401


async def test_push_endpoint_advances_and_acks(system):
    c, _, store, *_ = system
    await store.create(Run(id="pushed", workflow="triage", input={"alert": "a"}))
    r = await c.post(
        "/internal/advance", headers={"x-user": PUSH_SA}, json=envelope({"run_id": "pushed"})
    )
    assert r.status_code == 204
    assert (await store.get("pushed")).status is RunStatus.AWAITING_APPROVAL


async def test_push_during_active_work_is_nacked_not_duplicated(system):
    """A redelivered message that arrives while a worker holds the lease must not call the model twice."""
    c, dispatcher, _, llm, _ = system
    rid = await start(c, "a")
    r = await c.post(
        "/internal/advance", headers={"x-user": PUSH_SA}, json=envelope({"run_id": rid})
    )
    assert r.status_code in (204, 409)  # lost the race (409) or ran after it (204)
    await dispatcher.drain()
    assert len(llm.seen) == 1


async def test_push_endpoint_acks_unprocessable_messages_to_avoid_redelivery_loops(system):
    c, *_ = system
    h = {"x-user": PUSH_SA}
    assert (
        await c.post("/internal/advance", headers=h, json={"message": {"data": "!!!"}})
    ).status_code == 204
    assert (
        await c.post("/internal/advance", headers=h, json=envelope({"run_id": "ghost"}))
    ).status_code == 204


async def test_push_endpoint_nacks_when_step_fails_transiently(system):
    _, _, store, _, admin = system
    app, _ = build(1, store, admin)  # specialist port 1 is unreachable -> transient failure
    await store.create(Run(id="flaky", workflow="triage", input={"alert": "a"}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c2:
        r = await c2.post(
            "/internal/advance", headers={"x-user": PUSH_SA}, json=envelope({"run_id": "flaky"})
        )
    assert r.status_code == 503  # nack: Pub/Sub redelivers, the model result is not lost


class ApplyThenFailAdmin(FakeRunAdmin):
    """Reproduces the live failure: the change is applied, then the call raises (the real client
    errored while polling an operation it was not allowed to read)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.failed_once = False

    async def set_traffic(self, service, revision):
        await super().set_traffic(service, revision)
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("403 on operations.get after the update was accepted")


async def test_mutation_that_succeeds_but_raises_is_not_misreported_as_a_failure(specialist_server):
    port, _ = specialist_server
    store = InMemoryRunStore()
    admin = ApplyThenFailAdmin({"ao-demo-target": target_service()})
    app, dispatcher = build(port, store, admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        rid = await start(c)
        await dispatcher.drain()
        await c.post(f"/runs/{rid}/approvals/rollback", headers=op(), json={"approved": True})
        await (
            dispatcher.drain()
        )  # first delivery raises after mutating; the redelivery must succeed
        run = await get(c, rid)
    assert run["status"] == "succeeded", run["error"]
    assert run["steps"]["rollback"]["output"]["already_applied"] is True
    assert run["steps"]["verify"]["output"]["serving"] == "rev-2"
    assert admin.mutations == [("ao-demo-target", "rev-2")]  # one real mutation, not two


async def test_kill_switch_api_refuses_new_runs_and_reports_state(specialist_server):
    from agentorch.guard import InMemoryGuard

    port, _ = specialist_server
    store, admin = InMemoryRunStore(), FakeRunAdmin({"ao-demo-target": target_service()})
    app, dispatcher = build(port, store, admin, guard=InMemoryGuard(daily_cap_usd=5))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        assert (await c.get("/admin/state", headers=op())).json()["killswitch"] is False
        mallory = {"x-user": "mallory@evil.test"}
        assert (
            await c.post("/admin/killswitch", headers=mallory, json={"enabled": True})
        ).status_code == 403
        assert (
            await c.post("/admin/killswitch", headers=op(), json={"enabled": "yes"})
        ).status_code == 400

        assert (await c.post("/admin/killswitch", headers=op(), json={"enabled": True})).json()[
            "killswitch"
        ] is True
        r = await c.post(
            "/runs", headers=op(), json={"workflow": "triage", "input": {"alert": "x"}}
        )
        assert r.status_code == 503 and "kill switch" in r.json()["error"]

        await c.post("/admin/killswitch", headers=op(), json={"enabled": False})
        r = await c.post(
            "/runs", headers=op(), json={"workflow": "triage", "input": {"alert": "x"}}
        )
        assert r.status_code == 202
        await dispatcher.drain()


async def test_approved_rollback_is_blocked_if_the_kill_switch_is_pulled_during_the_wait(
    specialist_server,
):
    """The check sits before the step, so it also stops an already-approved infrastructure change."""
    from agentorch.guard import InMemoryGuard

    port, _ = specialist_server
    store, admin, guard = (
        InMemoryRunStore(),
        FakeRunAdmin({"ao-demo-target": target_service()}),
        InMemoryGuard(5),
    )
    app, dispatcher = build(port, store, admin, guard=guard)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://orch"
    ) as c:
        rid = await start(c)
        await dispatcher.drain()
        assert (await get(c, rid))["status"] == "awaiting_approval"
        await guard.set_killswitch(True, "ops")
        await c.post(f"/runs/{rid}/approvals/rollback", headers=op(), json={"approved": True})
        await dispatcher.drain()
        run = await get(c, rid)
    assert run["status"] == "halted" and admin.mutations == []


# --- input validation: malformed or hostile caller input must be a clean 4xx, never a 500 or a bad write ---
async def post_run(c, **body):
    return await c.post("/runs", headers=op(), json=body)


async def test_malformed_bodies_are_400_not_500(system):
    c, *_ = system
    r = await c.post(
        "/runs", headers={**op(), "content-type": "application/json"}, content=b"{not json"
    )
    assert r.status_code == 400
    r = await c.post(
        "/runs", headers={**op(), "content-type": "application/json"}, content=b'["a list"]'
    )
    assert r.status_code == 400
    assert (
        await c.post(
            "/admin/killswitch", headers={**op(), "content-type": "application/json"}, content=b"x"
        )
    ).status_code == 400


@pytest.mark.parametrize(
    "run_id", ["a/b", "../x", "x" * 64, "", " lead", "-lead", 123, ["a"], "emoji☃", "a b"]
)
async def test_run_ids_that_could_break_the_document_path_are_rejected(system, run_id):
    c, *_ = system
    r = await post_run(c, workflow="triage", input={"alert": "a"}, run_id=run_id)
    assert r.status_code == 400 and "run_id" in r.json()["error"]


async def test_input_must_be_a_small_json_object(system):
    c, *_ = system
    assert (await post_run(c, workflow="triage", input="a string")).status_code == 400
    assert (await post_run(c, workflow="triage", input=["a"])).status_code == 400
    big = await post_run(c, workflow="triage", input={"alert": "x" * 20_000})
    assert big.status_code == 413


@pytest.mark.parametrize(
    "budget",
    [
        {"max_tokens": "lots"},
        {"max_tokens": 0, "max_cost_usd": 1},
        {"max_tokens": -5},
        {"max_tokens": 10**9, "max_cost_usd": 1},
        {"max_tokens": 1000, "max_cost_usd": 1000.0},
        "free",
        [1, 2],
    ],
)
async def test_budgets_are_validated_and_capped(system, budget):
    c, *_ = system
    r = await post_run(c, workflow="triage", input={"alert": "a"}, budget=budget)
    assert r.status_code == 400 and "budget" in r.json()["error"]


async def test_a_sane_budget_and_run_id_are_accepted(system):
    c, dispatcher, *_ = system
    r = await post_run(
        c,
        workflow="triage",
        input={"alert": "a"},
        run_id="alert-2026-10-19_a1",
        budget={"max_tokens": 50_000, "max_cost_usd": 0.05},
    )
    assert r.status_code == 202 and r.json()["budget"]["max_cost_usd"] == 0.05
    await dispatcher.drain()


async def test_list_limit_is_parsed_and_clamped(system):
    c, *_ = system
    assert (await c.get("/runs?limit=abc", headers=op())).status_code == 400
    assert (await c.get("/runs?limit=100000", headers=op())).status_code == 200
    assert (await c.get("/runs?limit=-3", headers=op())).status_code == 200


async def test_viewer_page_is_served_with_the_platform_status_bar_and_no_inline_html_injection(
    system,
):
    c, *_ = system
    r = await c.get("/")
    assert r.status_code == 200 and 'id="platform"' in r.text and "/admin/state" in r.text
    # model/log text must only ever reach the DOM as text: the page must not use innerHTML anywhere
    assert (
        "innerHTML" not in r.text
        and "insertAdjacentHTML" not in r.text
        and "document.write" not in r.text
    )
