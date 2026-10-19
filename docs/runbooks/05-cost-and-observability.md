# 5. Cost and observability

## What it costs
One incident is roughly 1.4-3k input and 0.1-0.6k output tokens on `gemini-2.5-flash` ($0.30 / $2.50 per 1M tokens,
read 2026-10-18): about **$0.001-0.003**. Platform guards: per-run budget (default 200k tokens / $0.25), daily cap
`DAILY_BUDGET_USD` (default $1), and the kill switch. Gemini is called through Vertex AI, which is **not** part of
the GCP free tier; everything else sits inside free-tier quotas at this volume.

Free-tier notes (limits from <https://docs.cloud.google.com/free/docs/free-cloud-features>, read 2026-10-18):
- The orchestrator, analyst and MCP server run with **CPU always allocated** (`cpu_idle=false`) so the span exporter can
  run between requests. That draws on the *instance-based* free pool (240k vCPU-s, 450k GiB-s per month) while an
  instance is alive (up to ~15 idle minutes after a request). By my estimate (3 services x 1 vCPU x ~15 minutes alive per incident) roughly 90 incidents a month fit; set `cpu_idle = true`
  in `terraform/services.tf` to go back to request-based billing if you do not need traces.
- Unverified: whether Cloud Run's free compute applies in `us-central1`, and how Firestore's free quota scopes to the
  `(default)` database. Check **Billing > Reports** after a week.
- The Artifact Registry repo keeps the 3 newest images (cleanup policy) to stay under 0.5 GB.

## Per-step cost ledger (BigQuery)
Every completed step logs a `step_usage` row (ids, token counts, cost; never prompts or model text), routed by a log
sink to `agentorch_costs.run_googleapis_com_stdout`. Allow 1-3 minutes.
```sql
-- cost per run, newest first
SELECT jsonPayload.run_id AS run_id,
       SUM(CAST(jsonPayload.input_tokens AS INT64))  AS input_tokens,
       SUM(CAST(jsonPayload.output_tokens AS INT64)) AS output_tokens,
       SUM(CAST(jsonPayload.cost_usd AS FLOAT64))    AS cost_usd,
       MIN(timestamp) AS started
FROM `PROJECT.agentorch_costs.run_googleapis_com_stdout`
GROUP BY run_id ORDER BY started DESC LIMIT 20;

-- cost per day
SELECT DATE(timestamp) AS day, SUM(CAST(jsonPayload.cost_usd AS FLOAT64)) AS cost_usd, COUNT(DISTINCT jsonPayload.run_id) AS runs
FROM `PROJECT.agentorch_costs.run_googleapis_com_stdout` GROUP BY day ORDER BY day DESC;
```
The ledger total for a run equals the run's own `usage.cost_usd` (checked by the live suite). Token usage reported
across A2A is a lower bound for agents making several model calls; Vertex billing is the authority.

## Traces
One incident is a single Cloud Trace trace across the orchestrator, analyst and MCP server (ADK `invoke_agent`,
`call_llm`, `generate_content` spans included). Find it by run id:
```
Cloud Console > Trace > Trace explorer > filter: run.id = <run_id>
```
or via the API: `GET https://cloudtrace.googleapis.com/v1/projects/$PROJECT_ID/traces?filter=run.id:<run_id>`.
Log lines carry `logging.googleapis.com/trace`, so a log entry links to its trace.
Sampling is `always_on` on purpose (observed: only a handful of requests per hour were traced under the default
parent-based sampler, because Cloud Run hands most requests an unsampled parent). Spans from the Pub/Sub push request and from the analyst do not share a trace with the operator's
`POST /runs`; correlate them by `run.id`.

## Platform state
`scripts/ao.py state` shows today's spend counter and the kill switch (Firestore `platform/`).
