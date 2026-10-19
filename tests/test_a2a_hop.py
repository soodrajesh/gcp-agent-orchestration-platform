"""Two ADK agents talking over real A2A (HTTP + Agent Card discovery), model faked.

The 'specialist' is served by to_a2a() on an ephemeral local port; the 'router' consumes it via
RemoteA2aAgent. This proves the wiring we will deploy to two separate Cloud Run services."""

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from agentorch.fake_llm import FakeLlm


async def test_agent_card_is_discoverable(specialist_server):
    port, _ = specialist_server
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "incident_analyst"


async def _ask(agent, question: str) -> list[str]:
    runner = InMemoryRunner(agent=agent, app_name="t")
    session = await runner.session_service.create_session(app_name="t", user_id="u")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    texts: list[str] = []
    async for ev in runner.run_async(user_id="u", session_id=session.id, new_message=msg):
        if ev.content and ev.content.parts:
            texts += [p.text for p in ev.content.parts if p.text]
    return texts


def _remote(port: int) -> RemoteA2aAgent:
    return RemoteA2aAgent(
        name="incident_analyst",
        agent_card=f"http://127.0.0.1:{port}/.well-known/agent-card.json",
        description="Explains cloud cost spikes",
        use_legacy=False,
    )


async def test_remote_agent_answers_over_a2a(specialist_server):
    port, specialist_llm = specialist_server
    texts = await _ask(_remote(port), "Why did spend spike on Oct 12?")
    assert any("bad deploy" in t for t in texts)
    assert "spend spike" in specialist_llm.seen[0]  # the question really crossed the wire


async def test_router_llm_routes_to_remote_specialist(specialist_server):
    port, specialist_llm = specialist_server
    router_llm = FakeLlm(replies=[FakeLlm.transfer_to("incident_analyst")], seen=[])
    router = LlmAgent(
        name="router",
        model=router_llm,
        instruction="Route cost questions to incident_analyst.",
        sub_agents=[_remote(port)],
    )
    texts = await _ask(router, "Why did spend spike on Oct 12?")
    assert len(router_llm.seen) == 1  # router decided once
    assert len(specialist_llm.seen) == 1  # and the specialist, not the router, answered
    assert any("bad deploy" in t for t in texts)
