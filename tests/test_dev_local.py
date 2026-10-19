"""scripts/dev_local.py is advertised in the README; it broke once when the workflow signature changed and
nothing noticed. This keeps it honest: its canned proposal must pass the real validator against its fake cloud."""

import importlib.util
from pathlib import Path

from agentorch.remediation import parse_diagnosis, plan_rollback


def load():
    spec = importlib.util.spec_from_file_location(
        "dev_local", Path(__file__).parent.parent / "scripts" / "dev_local.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_dev_server_canned_proposal_validates_against_its_fake_cloud_run():
    mod = load()
    d = parse_diagnosis(mod.PROPOSAL)
    plan = await plan_rollback(d.action, mod.fake_cloud_run(), frozenset({"ao-demo-target"}))
    assert (plan.current, plan.target) == ("dev-rev-2", "dev-rev-1")


def test_dev_server_builds_the_current_triage_workflow():
    from agentorch.workflows.triage import build_triage

    mod = load()
    wf = build_triage(
        "http://127.0.0.1:1",
        authenticate=False,
        model="fake",
        admin=mod.fake_cloud_run(),
        allowed=frozenset({"ao-demo-target"}),
    )
    assert [s.key for s in wf.steps] == ["diagnose", "plan", "rollback", "verify"]
