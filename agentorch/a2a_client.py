"""Call another agent over A2A and report the tokens it spent.

Token usage rides in the A2A task metadata (`adk_usage_metadata`). The same task event is
re-delivered several times while the task progresses, so we take the *last* value instead of
summing. It describes the final model response, so for an agent that makes several LLM calls it
is a lower bound, not the bill; authoritative cost comes from the per-call log (Phase 4)."""

from __future__ import annotations

import httpx
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from .engine import StepResult
from .models import Usage
from .pricing import usage_for


class IdTokenAuth(httpx.Auth):
    """Attach a Google-signed ID token for `audience` (Cloud Run service-to-service auth)."""

    def __init__(self, audience: str) -> None:
        self._audience = audience

    def auth_flow(self, request: httpx.Request):
        import google.auth.transport.requests
        from google.oauth2 import id_token

        # fetch_id_token caches nothing itself; the metadata server answers in ~ms on Cloud Run.
        token = id_token.fetch_id_token(google.auth.transport.requests.Request(), self._audience)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


# Terminal or blocked A2A task states that are not an answer (interrupted states need a human or
# credentials we cannot supply from inside a workflow step).
_BAD_STATES = {
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
}


def _usage_from_metadata(meta: dict | None, model: str) -> Usage:
    um = ((meta or {}).get("a2a:response", {}).get("metadata", {}) or {}).get("adk_usage_metadata")
    if not um:
        return Usage()
    return usage_for(
        model, int(um.get("promptTokenCount", 0)), int(um.get("candidatesTokenCount", 0))
    )


async def ask_agent(
    base_url: str,
    question: str,
    *,
    name: str = "remote",
    model: str = "gemini-2.5-flash",
    authenticate: bool = False,
    timeout: float = 120.0,
) -> StepResult:
    """Send `question` to the A2A agent at `base_url`; return its text and the usage it reported.

    `model` is only used to price the reported tokens; the remote agent picks its own model."""
    client = httpx.AsyncClient(
        auth=IdTokenAuth(base_url) if authenticate else None, timeout=timeout
    )
    try:
        remote = RemoteA2aAgent(
            name=name,
            agent_card=f"{base_url.rstrip('/')}/.well-known/agent-card.json",
            httpx_client=client,
            use_legacy=False,
        )
        runner = InMemoryRunner(agent=remote, app_name="orchestrator")
        session = await runner.session_service.create_session(
            app_name="orchestrator", user_id="orch"
        )
        msg = types.Content(role="user", parts=[types.Part(text=question)])
        texts: list[str] = []
        last_meta: dict | None = None
        async for ev in runner.run_async(user_id="orch", session_id=session.id, new_message=msg):
            if ev.custom_metadata:
                last_meta = ev.custom_metadata
            if ev.content and ev.content.parts:
                texts = [p.text for p in ev.content.parts if p.text] or texts
        # A failed task still streams text (a draft answer, then the error message). Read the task
        # state: returning that text would present a failure, or an ungrounded draft, as an answer.
        state = (((last_meta or {}).get("a2a:response") or {}).get("status") or {}).get("state", "")
        if state in _BAD_STATES:
            raise RuntimeError(
                f"agent {name} task ended in {state}: {(texts[-1] if texts else '')[:200]}"
            )
        if not texts:
            raise RuntimeError(f"agent {name} at {base_url} returned no text")
        return StepResult(output=texts[-1], usage=_usage_from_metadata(last_meta, model))
    finally:
        await client.aclose()
