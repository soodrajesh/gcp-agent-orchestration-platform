"""How a run gets (re)advanced. Cloud: Pub/Sub push -> orchestrator. Local/tests: in-process."""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from .engine import Engine, LeaseHeldError, StepRetryError


class Dispatcher(Protocol):
    async def dispatch(self, run_id: str) -> None: ...


class PubSubDispatcher:
    def __init__(self, project: str, topic: str) -> None:
        from google.cloud import pubsub_v1

        self._client = pubsub_v1.PublisherClient()
        self._path = self._client.topic_path(project, topic)

    async def dispatch(self, run_id: str) -> None:
        data = json.dumps({"run_id": run_id}).encode()
        await asyncio.to_thread(lambda: self._client.publish(self._path, data).result(timeout=30))


class LocalDispatcher:
    """Mimics Pub/Sub redelivery: nack-able failures are retried, everything else ends the loop."""

    def __init__(self, engine: Engine, max_deliveries: int = 5, delay: float = 0.0) -> None:
        self._engine, self._max, self._delay = engine, max_deliveries, delay
        self.tasks: list[asyncio.Task] = []

    async def dispatch(self, run_id: str) -> None:
        self.tasks.append(asyncio.create_task(self._deliver(run_id)))

    async def _deliver(self, run_id: str) -> None:
        for _ in range(self._max):
            try:
                await self._engine.advance(run_id)
                return
            except (StepRetryError, LeaseHeldError):
                await asyncio.sleep(self._delay)

    async def drain(self) -> None:
        await asyncio.gather(*self.tasks)
        self.tasks.clear()
