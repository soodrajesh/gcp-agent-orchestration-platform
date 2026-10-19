# 2. Operate incidents

## Set up your shell
```bash
export ORCH_URL=...  OPERATOR_SA=...  PROJECT_ID=...  REGION=us-central1
```
You act as the `ao-operator` service account by impersonation (a human's own Google token cannot be
verified by Cloud Run, ADR 0002). Your user needs `roles/iam.serviceAccountTokenCreator` on it (Terraform
grants this to `admin_email`).

## File an alert
```bash
.venv/bin/python scripts/ao.py run "ALERT: 5xx above 50% on Cloud Run service ao-demo-target after the latest deploy"
```
Use `--run-id <id>` to make filing idempotent (id: letters, digits, `_`, `-`, up to 63 chars, starting
with a letter or digit). Input is limited to 16 KiB.

## Review and decide
```bash
.venv/bin/python scripts/ui_proxy.py        # run viewer on http://127.0.0.1:8080
.venv/bin/python scripts/ao.py show <run_id>
```
A run at **awaiting approval** shows: the analyst's diagnosis, the evidence it cited, and the **exact action**
("send 100% of traffic from X to Y"). Check before approving:
- Is the *current* revision the one that is actually failing?
- Is the *target* revision healthy? **Code checks that it exists, is ready and is older; it does not check
  its error history.** Look at the evidence.
- Evidence strings are the model's words; open the service's logs if the decision matters.

```bash
.venv/bin/python scripts/ao.py approve <run_id> rollback            # approve
.venv/bin/python scripts/ao.py approve <run_id> rollback --reject   # reject (terminal, nothing changes)
```
Approval re-validates against live state; if traffic moved meanwhile the run fails with
`changed while awaiting approval` and nothing is changed. If the rollback was already applied, the run
still succeeds (`already_applied`).

## Stop the platform
```bash
.venv/bin/python scripts/ao.py killswitch on     # new runs refused (503); in-flight runs stop before their next step
.venv/bin/python scripts/ao.py state             # kill switch, and today's spend vs the daily cap
.venv/bin/python scripts/ao.py killswitch off    # resume
```
The switch is checked *before* every step, so it also blocks a rollback a human already approved. The
daily cap (`DAILY_BUDGET_USD`, default $1) trips the same way. Halted runs are terminal (`halted`); file the
alert again after resuming.

## Run states
| State | Meaning | Action |
|---|---|---|
| `awaiting_approval` | gated step waiting for a human | approve or reject |
| `succeeded` | finished (a rollback, or "no action needed") | none |
| `failed` | see `error` (`plan: ...` = the model's proposal was refused) | runbook 3 |
| `rejected` | a human said no | none |
| `budget_exceeded` | per-run token/cost budget reached | raise `budget` on the next run (max 1M tokens, $5) |
| `halted` | kill switch or daily cap | runbook above |
