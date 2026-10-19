"""Local dev server: in-memory store, scripted (fake) model, NO AUTH. Never deploy this.

.venv/bin/python scripts/dev_local.py     # http://127.0.0.1:8080
"""

import asyncio

import uvicorn
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import LlmAgent

from agentorch.auth import NoAuth
from agentorch.dispatch import LocalDispatcher
from agentorch.engine import Engine
from agentorch.fake_llm import FakeLlm
from agentorch.service import Settings, create_app
from agentorch.store import InMemoryRunStore
from agentorch.workflows.triage import build_triage

SPECIALIST_PORT, UI_PORT = 8091, 8080


class ScriptedForever(FakeLlm):
    async def generate_content_async(self, llm_request, stream=False):
        self.replies.append("(scripted dev reply) Likely cause: an oversized VM left running.")
        async for r in super().generate_content_async(llm_request, stream):
            yield r


async def main() -> None:
    agent = LlmAgent(
        name="incident_analyst", model=ScriptedForever(), instruction="dev", description="dev"
    )
    spec = uvicorn.Server(
        uvicorn.Config(
            to_a2a(agent, host="127.0.0.1", port=SPECIALIST_PORT),
            port=SPECIALIST_PORT,
            log_level="warning",
        )
    )
    store = InMemoryRunStore()
    engine = Engine(
        store,
        {
            "triage": build_triage(
                f"http://127.0.0.1:{SPECIALIST_PORT}", authenticate=False, model="fake"
            )
        },
    )
    app = create_app(
        engine,
        store,
        LocalDispatcher(engine),
        NoAuth("dev@local"),
        Settings(frozenset({"dev@local"}), "unused"),
        {"triage"},
    )
    ui = uvicorn.Server(uvicorn.Config(app, port=UI_PORT, log_level="warning"))
    await asyncio.gather(spec.serve(), ui.serve())


asyncio.run(main())
