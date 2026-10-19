# ADR 0001: Durable multi-agent runtime

Status: accepted (2026-10-18)

## Decision
- **Agents:** Google ADK 2.x (`google-adk` 2.12). Each agent is its own Cloud Run service exposed
  with `to_a2a()`; peers consume it with `RemoteA2aAgent`. Discovery is the A2A Agent Card
  (`/.well-known/agent-card.json`). A2A spec v1.0.0. ADK marks its A2A support experimental, so it
  is pinned and covered by a real over-HTTP test (`tests/test_a2a_hop.py`).
- **Durability is ours, not the framework's.** `agentorch.engine` is a small checkpointed state
  machine over a `RunStore` (Firestore in prod, in-memory in tests). It is deliberately independent
  of ADK so it can be tested exhaustively for free.
- **Not Temporal / Cloud Workflows / LangGraph.** Temporal needs an always-on cluster (not free).
  Cloud Workflows free tier is 5,000 steps/month. LangGraph replays re-trigger LLM calls.
- **Guarantee, stated precisely:** a *completed* step is never re-executed. Only the step in
  flight when a worker dies can run twice (at-least-once), so step bodies must tolerate that.
- **Concurrency:** optimistic compare-and-set on `Run.version`; the lease is just fields on the run.
- **Models:** Gemini 2.5 Flash by default (Vertex or free Gemini API key). 3.x Flash prices double
  on 2027-01-01. OpenAI is a test-only provider, never deployed, opt-in via env flag.
- **Region:** us-central1 (Cloud Storage's free tier is US-only; Cloud Run free-tier regional
  scope is unverified and must be confirmed on the first deploy).

## Costs we know and don't
Known: GCP free-tier quotas (docs.cloud.google.com/free/docs/free-cloud-features, read 2026-10-18).
Unknown, to be measured: Gemini API free-tier rate limits; whether the $300 trial credit remains.
