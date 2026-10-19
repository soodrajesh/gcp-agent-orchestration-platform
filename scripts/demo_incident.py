"""Stage a real incident, then file the alert.

1. deploy a failing revision of ao-demo-target (a "bad release") that takes 100% of traffic
2. send real requests to it, so real 5xx responses and error logs exist in Cloud Logging
3. wait for the logs to be searchable, then start a triage run with an alert about it

ORCH_URL=... OPERATOR_SA=... PROJECT_ID=... REGION=... .venv/bin/python scripts/demo_incident.py
"""

import os
import subprocess
import sys
import time

import httpx

from agentorch.operator import OperatorToken, OrchestratorClient

PROJECT, REGION = os.environ["PROJECT_ID"], os.environ.get("REGION", "us-central1")
SERVICE = "ao-demo-target"


def sh(*args: str) -> str:
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    if r.returncode:
        sys.exit(f"command failed: {' '.join(args)}\n{r.stderr}")
    return r.stdout.strip()


def target_url() -> str:
    return sh(
        "gcloud",
        "run",
        "services",
        "describe",
        SERVICE,
        "--project",
        PROJECT,
        "--region",
        REGION,
        "--format=value(status.url)",
    )


def reset_healthy() -> None:
    """A known-good revision to roll back TO: new healthy revision, 100% traffic, tracking latest.
    `update` makes the revision; `update-traffic --to-latest` is needed because after a rollback
    traffic is pinned to a specific revision and a new revision would otherwise get none."""
    print("[0/3] resetting ao-demo-target to a healthy revision ...", flush=True)
    base = ["--project", PROJECT, "--region", REGION, "--quiet"]
    sh(
        "gcloud",
        "run",
        "services",
        "update",
        SERVICE,
        *base,
        f"--update-env-vars=FAIL=0,APP_VERSION=healthy-{int(time.time()) % 100000}",
    )
    sh("gcloud", "run", "services", "update-traffic", SERVICE, *base, "--to-latest")


def deploy_bad_release(tag: str) -> None:
    print(
        f"[1/3] deploying failing release {tag} of {SERVICE} (takes 100% of traffic) ...",
        flush=True,
    )
    sh(
        "gcloud",
        "run",
        "services",
        "update",
        SERVICE,
        "--project",
        PROJECT,
        "--region",
        REGION,
        f"--update-env-vars=FAIL=1,APP_VERSION={tag}",
        "--quiet",
    )


def generate_traffic(n: int = 12) -> int:
    url = target_url()
    tok = OperatorToken(os.environ["OPERATOR_SA"], url).get()
    bad = 0
    with httpx.Client(timeout=30) as c:
        for _ in range(n):
            bad += c.get(url, headers={"authorization": f"Bearer {tok}"}).status_code >= 500
            time.sleep(0.4)
    print(f"[2/3] sent {n} requests, {bad} returned 5xx", flush=True)
    return bad


def serving_revision() -> str:
    return sh(
        "gcloud",
        "run",
        "services",
        "describe",
        SERVICE,
        "--project",
        PROJECT,
        "--region",
        REGION,
        "--format=value(status.traffic[0].revisionName)",
    )


def wait_for_error_logs(revision: str | None = None, timeout: int = 150) -> None:
    """Wait until ERROR logs from `revision` (default: the one serving now) are searchable.
    Matching on the revision matters: a previous incident's errors are still inside the 10-minute
    window and would make a plain "any errors?" check pass before the new release has logged anything
    (found live: the analyst then correctly saw errors only on an *older* revision and proposed nothing)."""
    revision = revision or serving_revision()
    q = (
        f"resource.type=cloud_run_revision AND resource.labels.service_name={SERVICE} "
        f"AND resource.labels.revision_name={revision} "
        'AND severity>=ERROR AND timestamp>="{since}"'
    )
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    end = time.time() + timeout
    while time.time() < end:
        out = sh(
            "gcloud",
            "logging",
            "read",
            q.format(since=since),
            "--project",
            PROJECT,
            "--limit=1",
            "--format=value(timestamp)",
        )
        if out:
            print(f"      error logs from {revision} are searchable", flush=True)
            return
        time.sleep(10)
    sys.exit("error logs did not appear in time")


def file_alert(tag: str) -> str:
    c = OrchestratorClient(os.environ["ORCH_URL"], os.environ["OPERATOR_SA"])
    alert = (
        f"ALERT: 5xx error rate above 50% on Cloud Run service {SERVICE} "
        f"in {PROJECT}/{REGION}, started shortly after the latest deploy ({tag})."
    )
    run = c.start("triage", {"alert": alert})
    print(f"[3/3] alert filed -> run {run['id']}", flush=True)
    return run["id"]


if __name__ == "__main__":
    tag = f"v2-bad-{int(time.time()) % 100000}"
    reset_healthy()
    deploy_bad_release(tag)
    generate_traffic()
    wait_for_error_logs()
    print(file_alert(tag))
