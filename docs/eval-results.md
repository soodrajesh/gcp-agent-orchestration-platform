# Model evals: choosing the analyst model

The analyst is the only step that uses a model, so its quality decides how often a run is useful
and its failures decide how often a run is wasted. `evals/run.py` runs the **real** analyst
(ADK agent, real MCP client, real server code, scripted logs and a fake Cloud Run) with a **real**
model against 8 hand-written incident scenarios. Every reply goes through the production parser and
the live-state validator. Each scenario has exactly one acceptable outcome.

| Model | Runs | Correct | Unsafe proposals | Cost | Notes |
|---|---|---|---|---|---|
| `gemini-2.5-flash-lite` | 24 (8 scenarios x 3) | **13 (54%)** | 0 | $0.0084 | rejected as the default |
| `gemini-2.5-flash` (run 1) | 24 | **23 (96%)** | 0 | $0.0217 | the one miss was a network error (`Connection reset by peer`), not a wrong answer |
| `gemini-2.5-flash` (run 2) | 24 | **24 (100%)** | 0 | $0.0227 | after the harness retried transport errors once |
| `openai/gpt-5-nano` | 3 (subset) | **3 (100%)** | 0 | $0.0039 | cross-vendor check only: `bad_release`, `no_errors`, `injection_in_logs` |

**Decision:** the deployed analyst uses `gemini-2.5-flash` (47 of 48 across two runs, 0 unsafe), at
roughly $0.003 per incident.

## Why flash-lite failed (all of it failed closed)
Of its 11 misses: **6 produced no usable JSON**, namely an empty final reply, a prose answer, and
`print(default_api.list_revisions(...))` emitted as *text* instead of a real function call. **3**
proposed rolling back to a revision that was itself throwing errors (`newest_older_also_bad`), which
the validator cannot catch because that revision was ready and older. **2** proposed the revision
already serving, which the validator rejected. None named another service, and none reached a human
or Cloud Run.

## What this does and does not show
- **Safety held for every model and every scenario, including both injection scenarios:** 0 unsafe
  proposals in 75 runs (24 + 24 + 24 + 3). That is mostly the validator's doing, not the model's, which is the point of the design.
- **Quality is model-dependent and measured, not assumed.** The architecture alone would have shipped
  a 54% analyst without anyone noticing; the eval is what caught it.
- **The fixtures are hand-written and small (8 scenarios).** This measures behaviour on these cases,
  not general accuracy, and 100% on 8 cases is not a claim about production traffic.
- The OpenAI run is a 3-call sanity check (capped at 4,096 output tokens per call, opt-in, never
  deployed), not a benchmark. `openai/gpt-5-nano` is the cheapest current OpenAI text model on
  <https://developers.openai.com/api/docs/pricing> as read on 2026-10-18.
- The per-run JSON for `gemini-2.5-flash` run 1 was overwritten before per-model result files existed;
  its report text is summarised above. Run 2 and the other models' results are in
  [evals/results/](../evals/results/) and [docs/evals/](evals/).

## Reproduce
```bash
export GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global
EVAL_MODEL=gemini-2.5-flash EVAL_RUNS=3 .venv/bin/python evals/run.py     # ~6 minutes, ~$0.02
# cross-vendor (opt-in): needs the `openai` extra and a key; prints no secrets
OPENAI_API_KEY=... EVAL_MODEL=openai/gpt-5-nano EVAL_RUNS=1 EVAL_SCENARIOS=bad_release,no_errors .venv/bin/python evals/run.py
```
The script exits 1 on any unsafe proposal or accuracy below `EVAL_MIN_ACCURACY` (default 0.8), so it
can gate a release.
