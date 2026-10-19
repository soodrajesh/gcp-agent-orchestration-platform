"""FirestoreGuard against the real database (opt-in, like the store contract tests)."""

import os
import uuid

import pytest

from agentorch.models import Usage

pytestmark = pytest.mark.firestore


@pytest.fixture
async def guard():
    from google.cloud import firestore

    from agentorch.guard import FirestoreGuard

    client = firestore.AsyncClient(project=os.environ["AGENTORCH_FIRESTORE_PROJECT"])
    col = f"it_platform_{uuid.uuid4().hex[:8]}"
    yield FirestoreGuard(client, daily_cap_usd=0.01, collection=col)
    async for doc in client.collection(col).list_documents():
        await doc.delete()


async def test_kill_switch_round_trips(guard):
    assert await guard.halt_reason() is None
    await guard.set_killswitch(True, "tester")
    assert "kill switch" in await guard.halt_reason()
    assert (await guard.state())["killswitch"] is True
    await guard.set_killswitch(False, "tester")
    assert await guard.halt_reason() is None


async def test_spend_accumulates_atomically_and_trips_the_daily_cap(guard):
    await guard.record(Usage(cost_usd=0.004))
    await guard.record(Usage(cost_usd=0.004))
    assert abs((await guard.state())["spent_today_usd"] - 0.008) < 1e-9
    assert await guard.halt_reason() is None  # 0.008 < 0.01
    await guard.record(Usage(cost_usd=0.004))
    assert "daily spend cap" in await guard.halt_reason()


async def test_zero_cost_steps_write_nothing(guard):
    await guard.record(Usage(cost_usd=0.0))
    assert (await guard.state())["spent_today_usd"] == 0.0
