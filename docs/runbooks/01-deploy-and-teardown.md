# 1. Deploy, update, verify, tear down

## Prerequisites
`gcloud` (logged in, project set, billing linked), `terraform` >= 1.6, `bq`, Python 3.11+ with `uv`.
Copy `deploy.env.example` to `deploy.env` and set `PROJECT_ID`/`REGION`/`ADMIN_EMAIL` (anything you
omit is read from `gcloud config`). The Terraform state bucket `<project>-tfstate` must exist (it is shared
with other stacks in the project).

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
make test            # offline, free, no cloud calls
```

## Deploy or update
```bash
./scripts/up.sh
```
What it does, and why it is safe to re-run:
1. **Foundation** (only if the Artifact Registry repo does not exist yet): APIs, Firestore `(default)`,
   registry, service accounts, Pub/Sub + dead-letter queue, BigQuery ledger and log sink, IAM.
2. **Image** via Cloud Build as the dedicated `ao-build` identity (retries up to 3x while its IAM grants propagate).
3. **Services**: the four Cloud Run services, push subscription, tracing config.

Every Terraform step is *planned, listed, then applied from the saved plan file*. If a plan would destroy
anything, `up.sh` stops and tells you; re-run with `ALLOW_DESTROY=1` only if that is intended.

Expect on a **fresh** stack: the first operator call can return **403 for 1-2 minutes** (IAM propagation),
and the Firestore TTL policy takes several minutes to create.

## Verify
```bash
./scripts/test.sh          # offline suite, then the live suite (~10 min, ~$0.003)
```
The live suite deploys a real failing release of `ao-demo-target`, then checks the outcome independently
of the application (Cloud Run state, real HTTP requests, request logs, Audit Logs, BigQuery, Cloud Trace).
It leaves the kill switch off and the demo service healthy.

## Operate the demo service
```bash
.venv/bin/python scripts/demo_incident.py   # reset to healthy, deploy a failing release, file the alert
.venv/bin/python scripts/demo_reset.py      # heal the demo service
```
`ao-demo-target`'s revisions belong to these scripts; Terraform deliberately ignores its template, so
`up.sh` will not "fix" a live incident.

## Tear down
```bash
./scripts/down.sh            # prompts for the project id; --yes to skip
```
Shows the destroy plan and applies that saved plan. It removes services, Pub/Sub, Firestore (all run
history), the BigQuery dataset, the registry, the build bucket and the service accounts. It leaves the
shared state bucket and the enabled APIs.

**Known untested edge:** re-creating the Firestore `(default)` database *immediately* after deleting it may
be refused for a while (database ids are not instantly reusable). If `up.sh` fails on the database after a
`down.sh`, wait a few minutes and re-run.

## Rollback of a bad deploy of this platform
Services keep serving the previous revision when a new one fails to start (this happened during
development: a missing dependency, see ADR 0004). Read the failed revision's logs
(`gcloud logging read 'resource.type=cloud_run_revision AND severity>=ERROR'`), fix, rebuild, re-run `up.sh`.
