"""Run persistence with optimistic concurrency (compare-and-set on `version`)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Protocol

from .models import Run, utcnow


class NotFoundError(KeyError):
    pass


class ConflictError(RuntimeError):
    """Another writer saved this run first; reload and retry."""


class RunStore(Protocol):
    async def create(self, run: Run) -> Run: ...
    async def get(self, run_id: str) -> Run: ...
    async def list_recent(self, limit: int = 20) -> list[Run]: ...
    async def extend_lease(self, run_id: str, owner: str, expires: datetime) -> bool:
        """Heartbeat: push `lease_expires` out iff `owner` still holds the lease. Deliberately does
        NOT bump `version`, so it never makes the worker's own compare-and-set save conflict."""
        ...

    async def save(self, run: Run) -> Run:
        """Persist `run` iff the stored version equals `run.version`; returns it with version+1."""
        ...


class InMemoryRunStore:
    """Reference implementation; also what the unit tests use. Round-trips through JSON so tests
    catch anything that would not survive a real document store."""

    def __init__(self) -> None:
        self._docs: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: Run) -> Run:
        async with self._lock:
            if run.id in self._docs:
                raise ConflictError(f"run {run.id} already exists")
            self._docs[run.id] = run.model_dump_json()
            return run.model_copy(deep=True)

    async def get(self, run_id: str) -> Run:
        async with self._lock:
            try:
                return Run.model_validate_json(self._docs[run_id])
            except KeyError:
                raise NotFoundError(run_id) from None

    async def save(self, run: Run) -> Run:
        async with self._lock:
            try:
                current = Run.model_validate_json(self._docs[run.id])
            except KeyError:
                raise NotFoundError(run.id) from None
            if current.version != run.version:
                raise ConflictError(f"run {run.id}: stored v{current.version}, got v{run.version}")
            saved = run.model_copy(
                deep=True, update={"version": run.version + 1, "updated_at": utcnow()}
            )
            self._docs[run.id] = saved.model_dump_json()
            return saved

    async def list_recent(self, limit: int = 20) -> list[Run]:
        async with self._lock:
            runs = [Run.model_validate_json(d) for d in self._docs.values()]
        return sorted(runs, key=lambda r: r.created_at, reverse=True)[:limit]

    async def extend_lease(self, run_id: str, owner: str, expires: datetime) -> bool:
        async with self._lock:
            doc = self._docs.get(run_id)
            if doc is None:
                return False
            run = Run.model_validate_json(doc)
            if run.lease_owner != owner:
                return False
            run.lease_expires = expires  # version intentionally unchanged
            self._docs[run_id] = run.model_dump_json()
            return True
