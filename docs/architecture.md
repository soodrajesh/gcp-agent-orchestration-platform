# Architecture

[![Architecture](img/architecture.png)](img/architecture.svg)

<sub>Click for the vector version. Diagram source: [`diagrams/architecture.py`](diagrams/architecture.py) (regenerate with `python3 docs/diagrams/architecture.py && python3 docs/diagrams/render.py docs/img/architecture.svg docs/img/architecture.png`).</sub>

## Components (as deployed)

| Service | Identity | Can do | Cannot do |
|---|---|---|---|
| `ao-orchestrator` | `ao-orchestrator` | read/write run documents; publish to `ao-runs`; call the analyst; update **traffic of `ao-demo-target` only** | touch any other Cloud Run service; read logs |
| `ao-incident-analyst` | `ao-incident-analyst` | call Gemini; call the MCP server | read Firestore; change anything |
| `ao-ops-mcp` | `ao-ops-mcp` | read logs (project-wide `logging.viewer`, but the server only queries allow-listed services); read `ao-demo-target` | write anything |
| `ao-demo-target` | `ao-demo-target` | nothing (no permissions) | everything |
| Pub/Sub push | `ao-pubsub-push` | call `/internal/advance` | anything else |
| Operator | `ao-operator` (humans impersonate it) | call the orchestrator API | reach the MCP server or the analyst directly |
| Log sink | its own writer identity | write to the `agentorch_costs` BigQuery dataset | anything else |

Every service is private: Cloud Run IAM rejects unauthenticated calls, and the application then verifies the
token itself (signature, issuer, expiry and **audience**) and checks the caller against its own allow-list.

## One incident, end to end

The numbers match the diagram above.

1. **Operator -> orchestrator.** `POST /runs {alert}` with an audience-bound ID token minted by impersonating `ao-operator`.
   Input is validated (JSON object under 16 KiB, safe `run_id`, capped budget) and refused with `503` if the kill switch is on.
2. **Orchestrator -> Pub/Sub.** The run document is created in Firestore and its `run_id` published.
3. **Pub/Sub -> orchestrator.** The push subscription calls `/internal/advance` with an OIDC token; only `ao-pubsub-push`
   is accepted there. A message that fails 5 times lands in `ao-runs-dlq`.
4. **State.** The engine claims a lease (compare-and-set on `Run.version`), runs the next step, checkpoints, and repeats.
   A heartbeat extends the lease without bumping the version. Before each step it checks the per-run budget, the daily
   cap and the kill switch.
5. **`diagnose`: orchestrator -> analyst** over A2A with its own ID token.
6. **Analyst -> Vertex AI** (Gemini 2.5 Flash).
7. **Analyst -> MCP server** with its own ID token. It must call both `list_revisions` and `recent_errors`; an answer without
   them is refused, and a failed A2A task is raised by the client instead of being read as an answer.
8. **MCP server -> Cloud Logging** for ERROR lines of one allow-listed service (arguments validated and clamped). The text
   comes back flagged as untrusted.
9. **Target -> Cloud Logging.** The failing service writes the lines the analyst reads.
10. **`plan`, `rollback`, `verify`.** The analyst's JSON becomes a closed-schema proposal checked by code against live
    Cloud Run state; if it is a rollback, the run waits for a human. On approval the engine re-checks state, shifts 100% of
    traffic to the target revision (idempotent: already serving counts as done) and polls the service until it is true.
    This is the only action the platform can take.
11. **Cost ledger.** Each completed step logs a `step_usage` JSON line (ids, tokens, cost; never prompts or model text); a log
    sink routes it to a partitioned BigQuery table.
12. **Traces.** Spans for each step plus ADK's own, with trace context carried over A2A and MCP, so one incident is a single
    trace across three services.
13. **Identities.** One service account per service, scoped as in the table above.

## Run states

| State | Entered when | Next |
|---|---|---|
| `pending` | created, or released after a retryable failure or a human decision | `running` when a worker claims the lease |
| `running` | a worker holds the lease | any of the below |
| `awaiting_approval` | a gated step is reached with no decision | `pending` once a human decides |
| `succeeded` | every step finished or was skipped | terminal |
| `failed` | a permanent error (e.g. a refused proposal) or attempts exhausted | terminal |
| `rejected` | a human rejected the gated step | terminal |
| `budget_exceeded` | the run's token or cost budget is spent | terminal |
| `halted` | the kill switch is on or the daily cap is reached | terminal |

Every transition is a compare-and-set on `Run.version` in Firestore. The lease is two fields on the run document, owned
**per `advance()` call** (not per process), so concurrent requests on one Cloud Run instance exclude each other too.

## What each guarantee rests on

| Guarantee | Mechanism | Where |
|---|---|---|
| A finished step never runs again | step output stored in the run document; engine skips checkpointed steps | `engine.py` |
| Only the in-flight step can repeat | at-least-once delivery; all effects behind it are idempotent | `engine.py`, `remediation.py` |
| Two workers never advance one run | lease via CAS; owner unique per call; heartbeat keeps it alive | `engine.py`, `store.py` |
| The model cannot act | reply -> closed schema -> live-state validation, before any human sees it | `remediation.py` |
| A human decision is required for a change | approval gate; first decision wins; re-validated at execution | `engine.py`, `workflows/triage.py` |
| Spend is bounded | per-run budget, platform daily cap, kill switch; all checked *before* the step | `engine.py`, `guard.py` |
| Identity is verified twice | Cloud Run IAM, then signature + issuer + expiry + **audience** in the app | `auth.py` |
| Every step is costed and traced | JSON `step_usage` log -> BigQuery; spans -> Cloud Trace | `obs.py`, `terraform/observability.tf` |

## How each part fails

| Failure | Behaviour |
|---|---|
| Worker crashes mid-step | lease expires; Pub/Sub redelivers; only that step re-runs |
| Duplicate / redelivered message | lease held -> `409` nack (retry later); terminal run -> ack no-op |
| Analyst or MCP unreachable | step raises -> `503` nack -> redelivery; after 3 attempts the run fails |
| Vertex `429 RESOURCE_EXHAUSTED` | the failed A2A task is raised, the step retries (seen live under bursty load) |
| Analyst answers without using its tools | refused inside the agent; the A2A client raises on `TASK_STATE_FAILED` |
| Model returns bad / hostile JSON | `plan` fails the run before any approval is requested |
| Traffic moved while waiting for approval | execution refuses (`changed while awaiting approval`) |
| Rollback applied but the call errored | the retry sees the target already serving and succeeds (`already_applied`) |
| Poison message | 5 deliveries, then `ao-runs-dlq` |
| Spend or runaway | per-run budget -> `budget_exceeded`; daily cap or kill switch -> `halted` |
| Trace export blocked | spans are lost; requests are not affected |

## Deployment

Terraform (`terraform/`) creates the APIs, Firestore `(default)` with TTL policies, the registry with a cleanup policy,
the Pub/Sub topic, push subscription and dead-letter queue, the four Cloud Run services (scale to zero, max 2 instances),
every service account and IAM binding, the build identity and its staging bucket, and the BigQuery dataset with the log
sink. `scripts/up.sh` plans, prints the resource list, refuses unexpected destroys, and applies the **saved plan file**.
The three traced services run with CPU always allocated and `OTEL_TRACES_SAMPLER=always_on` (see
[ADR 0004](adr/0004-evals-guards-observability.md)); `ao-demo-target`'s revisions belong to the demo scripts, and Terraform
ignores its template on purpose.
