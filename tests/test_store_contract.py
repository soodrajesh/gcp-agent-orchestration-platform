"""One contract, run against every RunStore. The Firestore variant is opt-in (real database)."""

import pytest

from agentorch.models import Run, RunStatus, Usage
from agentorch.store import ConflictError, NotFoundError


def mk(i="r1"):
    return Run(id=i, workflow="wf", input={"k": "v", "n": [1, 2]})


async def test_create_then_get_roundtrips(store):
    await store.create(mk())
    got = await store.get("r1")
    assert got.input == {"k": "v", "n": [1, 2]} and got.status is RunStatus.PENDING


async def test_create_duplicate_conflicts(store):
    await store.create(mk())
    with pytest.raises(ConflictError):
        await store.create(mk())


async def test_get_missing(store):
    with pytest.raises(NotFoundError):
        await store.get("nope")


async def test_save_bumps_version_and_persists(store):
    run = await store.create(mk())
    run.usage = Usage(input_tokens=5, cost_usd=0.01)
    saved = await store.save(run)
    assert saved.version == run.version + 1
    assert (await store.get("r1")).usage.input_tokens == 5


async def test_stale_save_conflicts(store):
    run = await store.create(mk())
    a, b = await store.get("r1"), await store.get("r1")
    await store.save(a)
    with pytest.raises(ConflictError):
        await store.save(b)
    assert run.version == 0


async def test_expires_at_is_stored_as_a_native_timestamp(store):
    """Firestore TTL ignores string fields, so a string here would mean runs never expire."""
    from datetime import datetime

    if not hasattr(store, "_col"):
        pytest.skip("Firestore-specific: TTL needs a native timestamp")
    await store.create(mk())
    snap = await store._col.document("r1").get()
    assert isinstance(snap.to_dict()["expires_at"], datetime)


async def test_list_recent_is_newest_first_and_limited(store):
    import asyncio

    for i in range(3):
        await store.create(mk(f"r{i}"))
        await asyncio.sleep(0.01)
    assert [r.id for r in await store.list_recent(limit=2)] == ["r2", "r1"]


async def test_extend_lease_extends_for_the_owner_only_and_never_bumps_version(store):
    """The heartbeat's store operation, against every store (in production it never fired in the
    short live runs, so this is the only real-Firestore coverage)."""
    from datetime import timedelta

    from agentorch.models import utcnow

    run = mk("lease")
    run.lease_owner, run.lease_expires = "me", utcnow() + timedelta(seconds=5)
    created = await store.create(run)
    later = utcnow() + timedelta(seconds=300)
    assert await store.extend_lease("lease", "me", later) is True
    got = await store.get("lease")
    assert got.lease_expires >= later - timedelta(seconds=1)
    assert (
        got.version == created.version
    )  # the worker's own compare-and-set save must still succeed
    assert await store.extend_lease("lease", "someone-else", later + timedelta(seconds=60)) is False
    assert await store.extend_lease("missing", "me", later) is False
    saved = await store.save(got)  # and the worker can still save afterwards
    assert saved.version == created.version + 1
