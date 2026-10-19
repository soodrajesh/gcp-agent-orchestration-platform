# 4. Extend the platform

## Add a workflow
A workflow is an ordered tuple of `Step`s (`agentorch/engine.py`); see `workflows/triage.py`.
- A step is `async fn(ctx) -> StepResult(output, usage)`. Its output must be JSON-serialisable.
- `approval_prompt=` makes it wait for a human; `skip_if=` skips it (checkpointed) without a prompt.
- Raise `PermanentStepError` when retrying cannot help (invalid input, refused proposal); any other
  exception is retried up to `max_attempts`.
- **Anything that changes the world must be idempotent**: steps are at-least-once. See `execute_rollback`.
- Register it in `service.build_from_env()` and the `workflows` set passed to `create_app`.
- Write the offline tests first (fake model, fake admin). `tests/test_service.py` shows the pattern.

## Add a tool to the MCP server
`agentorch/mcp_ops.py`: add an `@mcp.tool()` inside `build_server`. Rules that this repo enforces:
validate every argument (regex / bounds, as `check()` and the clamps do); keep it read-only; return data the
model must treat as untrusted (say so in the result); never log argument values. Add tests in
`tests/test_mcp_ops.py` for the hostile-input cases. If the tool needs new IAM, grant it to `ao-ops-mcp` on the
specific resource, not the project.

## Let the platform act on another service
Three places, all deliberate:
1. `local.rollback_allowed` in `terraform/services.tf` (comma-separated; feeds `ROLLBACK_ALLOWED_SERVICES` and
   `OPS_ALLOWED_SERVICES`).
2. IAM on that service: `run.developer` for `ao-orchestrator`, `run.viewer` for `ao-ops-mcp`, and
   `serviceAccountUser` on its runtime service account (copy the three `demo_target` blocks in `services.tf`).
3. The `SERVICE_RE` regex in `remediation.py`/`mcp_ops.py` already admits normal Cloud Run names.
Then add an eval scenario for it.

## Change the model
1. Add pricing to `agentorch/pricing.py` (per 1M tokens; cite the source and date).
2. **Run the eval before deploying**: `EVAL_MODEL=<model> EVAL_RUNS=3 .venv/bin/python evals/run.py`. It exits 1
   on any unsafe proposal or accuracy below `EVAL_MIN_ACCURACY`. The default model was chosen this way
   (`gemini-2.5-flash-lite` scored 54%, `gemini-2.5-flash` 96-100%; `docs/eval-results.md`).
3. Set `agent_model` in Terraform (or `TF_VAR_agent_model`) and `./scripts/up.sh`.
Non-Gemini models go through ADK's LiteLlm adapter (`pip install -e ".[openai]"`); this is for evals only and is
never installed in the deployed image.

## Add an eval scenario
`evals/scenarios.py`: a `Scenario` with revisions (newest first), the ERROR log lines the tool returns, and the single
acceptable outcome. Add the cases that scare you: ambiguous history, hostile text, nothing to roll back to.

## Use a different MCP server (e.g. `gcp-mcp-platform`)
Set the analyst's `MCP_URL`, and register the analyst service account in *that* server's caller policy with the
`ops:read` scope. The analyst expects tools named `recent_errors` and `list_revisions`.
