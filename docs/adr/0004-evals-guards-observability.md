# ADR 0004: Evals pick the model; guards bound the platform; observability had to be debugged live

Status: accepted (2026-10-19). Most of the "why" below comes from things that broke against real infrastructure.

## 1. The model is chosen by an eval, not by price
`evals/run.py` runs the real analyst against 8 scripted incidents (ambiguous history, an unready target,
false alarms, two prompt injections) and scores each reply through the production parser and validator.
`gemini-2.5-flash-lite`, the cheapest option and the first default, scored **13/24 (54%)**: it emitted
empty replies, prose, and tool calls printed as text, and proposed a rollback to a revision that was itself failing.
`gemini-2.5-flash` scored 23/24 then 24/24, at about $0.003 per incident. It is now the default.
Safety did not depend on the model: 0 unsafe proposals in 75 runs across three models/vendors (a 3-call
`openai/gpt-5-nano` cross-check was included, opt-in and never deployed). Full table: `docs/eval-results.md`.
The harness gates releases (exit 1 on any unsafe proposal or accuracy under 80%).

## 2. Platform-level guards (`guard.py`)
Per-run budgets bound one run. Two more controls bound the platform, both checked **before every step that would
execute**, including an already-approved infrastructure step: a daily USD cap and a kill switch, kept in two
Firestore documents (`platform/killswitch`, `platform/spend-YYYYMMDD`, TTL 3 days). A tripped guard ends the run
`halted`; nothing is half-applied. The switch is operable with `scripts/ao.py killswitch on|off`.

## 3. Lease heartbeat, without version bumps
A slow model call could outlive the 300 s lease and be taken over as if the worker had died. A background task
now extends the lease every third of the lease period (`extend_lease`), as a store operation that checks the owner
and does **not** bump `Run.version`, so it can never make the worker's own compare-and-set save conflict. Tested with
a 0.3 s lease and a 0.9 s step, with a control proving the lease really does lapse without it.

## 4. Observability, and what it took
- **Cost ledger:** the engine logs one JSON `step_usage` line per step (ids, tokens, cost; never prompts, tested);
  a log sink routes them to a partitioned BigQuery table. The live suite checks the ledger total equals the run's cost.
- **Tracing:** spans for each step plus ADK's own, with httpx and Starlette instrumented, so one trace follows a run
  across orchestrator, analyst and MCP server. Getting there exposed four separate problems that offline tests could not:
  1. *Every service crashed at startup.* ADK's Cloud Trace exporter needs `opentelemetry-exporter-otlp-proto-http`,
     which we had not declared. All tests replaced the tracer, so nothing built the real exporter. Fixed, plus a test that does.
  2. *Spans were rejected (HTTP 400).* We replaced ADK's OTel resource with a bare one, dropping `gcp.project_id`.
  3. *Nothing arrived, silently.* Cloud Run hands most requests an unsampled parent and the default parent-based sampler
     obeys it; only a handful of requests per hour were traced. `OTEL_TRACES_SAMPLER=always_on` fixed it.
  4. *Background export is throttled* with request-based CPU, so the three traced services run with CPU always allocated
     (a free-tier trade-off, documented in runbook 5).

## 5. A missing tool must not produce an answer
ADK logs a warning and runs the agent **without** a toolset that failed to load, and the model may still answer.
The analyst now fails its task unless it called both tools, and the A2A client raises on a `FAILED` task state
instead of reading the error message as the answer (a failed task streams a draft answer, then the error text, then
`TASK_STATE_FAILED`; taking the last text would have returned the error as if it were a diagnosis).
Honest note: this was prompted by a startup warning that turned out to be ADK building the agent card without
credentials, not by a run that lacked tools; the guard is defence in depth, not a fix for an observed failure.

## 6. Findings from the security audit (fixed)
- `run_id` was used as a Firestore document id without validation; `input` and `budget` were unbounded; malformed JSON
  and a non-integer `limit` were 500s. All are now validated (regex, 16 KiB, capped budget) and tested.
- The local UI proxy attaches the operator's token to anything that reaches it. It now rejects foreign `Host`
  (DNS rebinding), cross-origin / cross-site requests, and non-JSON writes (a no-preflight CSRF).
- `up.sh` applied the "foundation" phase with an empty image, which makes the Cloud Run services `count = 0`; re-running it
  on a live stack would have planned to **destroy every service**. It now runs that phase only on a fresh project and
  refuses unexpected destroys. `down.sh` uses a reviewed saved plan.

## Consequences
- Cost per incident is about 3x the flash-lite price; accepted, because the cheaper model was wrong about half the time.
- Always-on CPU spends the instance-based free pool; `cpu_idle = true` restores request-based billing at the cost of traces.
- Open: the validator does not check the *target* revision's error history; evidence strings are not cross-checked against logs.
