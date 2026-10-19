# Architecture

## Components (as deployed)

```mermaid
flowchart LR
  op[Operator<br/>impersonates ao-operator] -->|alert, approvals, kill switch| orch
  subgraph CR[Cloud Run - private, IAM + in-app token checks]
    orch[ao-orchestrator<br/>API, run engine, viewer]
    an[ao-incident-analyst<br/>ADK agent, A2A server]
    mcp[ao-ops-mcp<br/>read-only MCP server]
    tgt[ao-demo-target<br/>the service with incidents]
  end
  orch <-->|runs, checkpoints, spend, kill switch| fs[(Firestore)]
  orch -->|run_id| ps{{Pub/Sub ao-runs}}
  ps -->|push, OIDC| orch
  ps -.->|after 5 failed deliveries| dlq{{ao-runs-dlq}}
  orch -->|A2A + ID token| an
  an -->|Gemini 2.5 Flash| vx[Vertex AI]
  an -->|MCP + ID token| mcp
  mcp -->|logs, revisions| tgt
  orch -->|traffic update: this service only| tgt
  orch -. step_usage logs .-> sink[[Log sink]] --> bq[(BigQuery cost ledger)]
  orch & an & mcp -. spans .-> tr[(Cloud Trace)]
```

| Service | Identity | Can do | Cannot do |
|---|---|---|---|
| `ao-orchestrator` | `ao-orchestrator` | read/write run documents; publish to `ao-runs`; call the analyst; update **traffic of `ao-demo-target` only** | touch any other Cloud Run service; read logs |
| `ao-incident-analyst` | `ao-incident-analyst` | call Gemini; call the MCP server | read Firestore; change anything |
| `ao-ops-mcp` | `ao-ops-mcp` | read logs (project-wide `logging.viewer`, but the server only queries allow-listed services); read `ao-demo-target` | write anything |
| `ao-demo-target` | `ao-demo-target` | nothing (no permissions) | everything |
| Pub/Sub push | `ao-pubsub-push` | call `/internal/advance` | anything else |
| Operator | `ao-operator` (humans impersonate it) | call the orchestrator API | reach the MCP server or the analyst directly |

## One incident, end to end

```mermaid
sequenceDiagram
  autonumber
  participant O as Operator
  participant R as Orchestrator
  participant P as Pub/Sub
  participant A as Analyst (Gemini)
  participant M as MCP server
  participant C as Cloud Run API
  O->>R: POST /runs {alert}
  R->>P: publish run_id
  P->>R: push /internal/advance
  R->>A: A2A: "Alert: ..."
  A->>M: list_revisions, recent_errors
  M->>C: read revisions, read logs
  A-->>R: JSON proposal (untrusted)
  Note over R: plan: parse into closed schema,<br/>check against LIVE Cloud Run state
  R->>R: checkpoint, status = awaiting_approval
  O->>R: POST approval (human decision)
  R->>P: publish run_id
  P->>R: push /internal/advance
  R->>C: re-validate, shift 100% traffic
  R->>C: verify (poll service state)
  R-->>O: succeeded
```

## Run state machine

```mermaid
stateDiagram-v2
  [*] --> pending
  pending --> running: lease claimed
  running --> awaiting_approval: gated step reached
  awaiting_approval --> pending: human decided
  running --> pending: step failed, retries left
  running --> succeeded
  running --> failed: permanent error / attempts exhausted
  running --> rejected: human rejected
  running --> budget_exceeded: per-run budget
  running --> halted: kill switch / daily cap
  succeeded --> [*]
  failed --> [*]
  rejected --> [*]
  budget_exceeded --> [*]
  halted --> [*]
```

Every transition is a compare-and-set on `Run.version` in Firestore. The lease is two fields on the
run document, owned **per `advance()` call** (not per process), and kept alive by a heartbeat that
extends it without bumping `version`.

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
| Analyst answers without using its tools | refused inside the agent; A2A task state `FAILED` is raised by the client (not read as an answer) |
| Model returns bad / hostile JSON | `plan` fails the run before any approval is requested |
| Traffic moved while waiting for approval | execution refuses (`changed while awaiting approval`) |
| Rollback applied but the call errored | retry sees the target already serving and succeeds (`already_applied`) |
| Poison message | 5 deliveries, then `ao-runs-dlq` |
| Spend or runaway | per-run budget -> `budget_exceeded`; daily cap or kill switch -> `halted` |
| Trace export blocked | spans are lost, requests are not affected |
