import asyncio
import os
import uuid

import pytest
import uvicorn
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import LlmAgent
from helpers import PROPOSAL, free_port

from agentorch.fake_llm import FakeLlm
from agentorch.store import InMemoryRunStore


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "firestore" in item.keywords and not os.environ.get("AGENTORCH_FIRESTORE_PROJECT"):
            item.add_marker(pytest.mark.skip(reason="set AGENTORCH_FIRESTORE_PROJECT to run"))
        if "live_openai" in item.keywords and not (
            os.environ.get("AGENTORCH_LIVE_OPENAI") == "1" and os.environ.get("OPENAI_API_KEY")
        ):
            item.add_marker(
                pytest.mark.skip(reason="opt-in: AGENTORCH_LIVE_OPENAI=1 + OPENAI_API_KEY")
            )


@pytest.fixture(params=["memory", pytest.param("firestore", marks=pytest.mark.firestore)])
async def store(request):
    """Every RunStore implementation, same contract tests. Firestore uses a throwaway collection."""
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    from google.cloud import firestore

    from agentorch.firestore_store import FirestoreRunStore

    client = firestore.AsyncClient(project=os.environ["AGENTORCH_FIRESTORE_PROJECT"])
    col = f"it_runs_{uuid.uuid4().hex[:8]}"
    yield FirestoreRunStore(client, collection=col)
    async for doc in client.collection(col).list_documents():
        await doc.delete()


@pytest.fixture
async def specialist_server():
    llm = FakeLlm(replies=[PROPOSAL], seen=[])
    agent = LlmAgent(
        name="incident_analyst",
        model=llm,
        instruction="Analyse cloud cost spikes.",
        description="Diagnoses incidents",
    )
    port = free_port()
    app = to_a2a(agent, host="127.0.0.1", port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield port, llm
    server.should_exit = True
    await task
