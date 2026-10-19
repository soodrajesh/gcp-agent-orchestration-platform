# 3. Troubleshooting

Start with the run: `.venv/bin/python scripts/ao.py show <run_id>` (look at `status`, `error`, `steps`,
`attempts`). Then the logs: every step failure is logged with the run id and step.

```bash
gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=ao-orchestrator AND severity>=ERROR' \
  --project "$PROJECT_ID" --freshness=1h --limit=20 --format='value(timestamp,textPayload,jsonPayload.message)'
```

## A call returns 403
| Symptom | Cause | Fix |
|---|---|---|
| 403 "not authenticated" from `curl` | no token | call through `scripts/ao.py` / `scripts/ui_proxy.py` |
| 401 `bad token` from the API | a human's own Google token (Cloud Run alters it) or wrong audience | use the operator identity (runbook 2); never `gcloud auth print-identity-token` |
| 403 on the first call after `up.sh` | IAM propagation, 1-2 min | wait and retry |
| `forbidden` from the app | caller is not in `OPERATORS` / `PUSH_SA` / `MCP_CALLERS` | check the Terraform env for that service |
| the operator gets 403 from the MCP server or the analyst | by design: only the analyst may call MCP, only the orchestrator the analyst | none |

## A run failed
| `error` starts with | Meaning | What to do |
|---|---|---|
| `plan: model reply contained no JSON proposal` | the model did not return the JSON object | transient: file the alert again; if frequent, check the model (runbook 4) and `docs/eval-results.md` |
| `plan: model proposal does not match the allowed schema` | the model proposed an action that does not exist | working as designed; inspect `steps.diagnose` |
| `plan: service '...' is not allow-listed` | the model (or injected text) named another service | working as designed. Look at the alert/logs for injected instructions |
| `plan: revision '...' does not exist` / `is not ready` / `already serving` / `not older` | the proposal does not match live Cloud Run | working as designed; the model misread `list_revisions` |
| `diagnose: ... task ended in TASK_STATE_FAILED: analyst answered without calling` | the analyst could not use its tools (MCP unreachable) | check `ao-ops-mcp` logs and the analyst's `MCP_URL`; the step retries 3x first |
| `diagnose: ... 429 RESOURCE_EXHAUSTED` (in logs, `attempt=1/3`) | Vertex AI rate limit under bursty load (seen live when many runs start at once) | automatic: the failed A2A task is raised, Pub/Sub redelivers and the step retries (3 attempts). If it persists, stagger alerts or request a Vertex quota increase for the model |
| `rollback: ... changed while awaiting approval` | traffic moved to a *different* revision while the run waited | re-assess the incident and file a new alert |
| `rollback: ... is not allow-listed` | allow-list changed since the plan | re-file |
| `verify: verification failed` | traffic did not end up 100% on the target | check Cloud Run; the rollback may have been partially applied by hand |

A run stuck in `running`: the lease is 300 s and is kept alive by a heartbeat; if a worker died, wait for
the lease to lapse (Pub/Sub redelivers) rather than editing Firestore.

## Messages in the dead-letter queue
```bash
gcloud pubsub subscriptions pull ao-runs-dlq-sub --project "$PROJECT_ID" --limit=5 --format='value(message.data)' | base64 -d
```
A message lands there after 5 failed deliveries. The payload is only `{"run_id": ...}`; inspect that run.

## A service does not start after deploy
Cloud Run keeps the previous revision serving. Read the new revision's logs:
```bash
gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=<svc> AND severity>=ERROR' --limit=5 --freshness=30m
```
Seen in development: a missing `opentelemetry-exporter-otlp-proto-http` crashed all three services at
import. `tests/test_obs.py::test_every_service_entrypoint_imports_with_tracing_dependencies` now guards it.

## Observability looks empty
| Symptom | Cause / fix |
|---|---|
| no traces | check `ENABLE_TRACING=1`, `OTEL_TRACES_SAMPLER=always_on` (observed: Cloud Run hands most requests an unsampled parent), CPU always allocated (`cpu_idle=false`), roles `telemetry.tracesWriter`; the trace filter is `run.id:<run_id>` |
| `Failed to export span batch code: 400` | the OTel resource lacks `gcp.project_id` (fixed in `obs.py`) |
| ledger table missing / no rows | the log sink needs 1-3 minutes; table is `agentorch_costs.run_googleapis_com_stdout` |
| `Agent <unknown> will run without the tools from toolset McpToolset` at **cold start** | harmless: ADK builds the A2A agent card at startup without credentials. A run that really lacks tools is refused (`analyst answered without calling`) |

## Firestore
Collections: `runs` (TTL 30 days on `expires_at`), `platform` (`killswitch`, `spend-YYYYMMDD`, TTL 3 days).
Do not hand-edit run documents: writes use compare-and-set on `version`.
