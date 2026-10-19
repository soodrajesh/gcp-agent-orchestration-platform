"""Firestore-backed RunStore. Free tier: 1 GiB, 50k reads / 20k writes per day per project.

Every save is one transactional write, so a run costs roughly one write per step."""

from __future__ import annotations

from google.cloud import firestore

from .models import Run, utcnow
from .store import ConflictError, NotFoundError


def _doc(run: Run) -> dict:
    """JSON-ish document, but `expires_at` stays a native datetime: Firestore TTL only acts on
    timestamp-typed fields, and mode="json" would have turned it into a string."""
    return {**run.model_dump(mode="json"), "expires_at": run.expires_at}


class FirestoreRunStore:
    def __init__(self, client: firestore.AsyncClient, collection: str = "runs") -> None:
        self._col = client.collection(collection)
        self._client = client

    async def create(self, run: Run) -> Run:
        ref = self._col.document(run.id)
        try:
            await ref.create(_doc(run))
        except Exception as exc:  # google.api_core.exceptions.AlreadyExists
            if exc.__class__.__name__ == "AlreadyExists":
                raise ConflictError(f"run {run.id} already exists") from exc
            raise
        return run

    async def get(self, run_id: str) -> Run:
        snap = await self._col.document(run_id).get()
        if not snap.exists:
            raise NotFoundError(run_id)
        return Run.model_validate(snap.to_dict())

    async def list_recent(self, limit: int = 20) -> list[Run]:
        # created_at is stored as a UTC ISO-8601 string, which sorts chronologically.
        q = self._col.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [Run.model_validate(d.to_dict()) async for d in q.stream()]

    async def extend_lease(self, run_id: str, owner: str, expires) -> bool:
        ref = self._col.document(run_id)

        @firestore.async_transactional
        async def _txn(txn: firestore.AsyncTransaction) -> bool:
            snap = await ref.get(transaction=txn)
            if not snap.exists or snap.to_dict().get("lease_owner") != owner:
                return False
            txn.update(ref, {"lease_expires": expires.isoformat()})  # no version bump
            return True

        return await _txn(self._client.transaction())

    async def save(self, run: Run) -> Run:
        ref = self._col.document(run.id)
        saved = run.model_copy(
            deep=True, update={"version": run.version + 1, "updated_at": utcnow()}
        )

        @firestore.async_transactional
        async def _txn(txn: firestore.AsyncTransaction) -> None:
            snap = await ref.get(transaction=txn)
            if not snap.exists:
                raise NotFoundError(run.id)
            if snap.to_dict().get("version") != run.version:
                raise ConflictError(f"run {run.id}: version mismatch")
            txn.set(ref, _doc(saved))

        await _txn(self._client.transaction())
        return saved
