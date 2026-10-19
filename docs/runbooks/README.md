# Runbooks

| # | When you need to... | Runbook |
|---|---|---|
| 1 | Deploy, update, verify or tear down the stack | [01-deploy-and-teardown.md](01-deploy-and-teardown.md) |
| 2 | Handle an incident: file an alert, review, approve, reject, stop the platform | [02-operate-incidents.md](02-operate-incidents.md) |
| 3 | Work out why a run failed, hung, or a call returned 403 | [03-troubleshooting.md](03-troubleshooting.md) |
| 4 | Add a workflow, a tool, an allow-listed service, or change the model | [04-extend.md](04-extend.md) |
| 5 | Look at cost, traces and the ledger; stay inside the free tier | [05-cost-and-observability.md](05-cost-and-observability.md) |

Conventions: commands run from the repository root; `ORCH_URL`, `OPERATOR_SA`, `PROJECT_ID`,
`REGION` come from `./scripts/up.sh` output (or `terraform -chdir=terraform output`).
