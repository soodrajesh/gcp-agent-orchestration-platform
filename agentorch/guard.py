"""Platform-wide spend guard and kill switch.

Per-run budgets (Run.budget) bound one run. This bounds the whole platform: a daily USD cap and a
manual kill switch, both checked before every step that would execute. A tripped guard ends the run
as HALTED; nothing is deleted and nothing is half-applied, because the check sits *before* the step,
including before an approved infrastructure step."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Protocol

from .models import Usage, utcnow


class SpendGuard(Protocol):
    async def halt_reason(self) -> str | None: ...
    async def record(self, usage: Usage) -> None: ...
    async def set_killswitch(self, enabled: bool, by: str) -> None: ...
    async def state(self) -> dict: ...


class NullGuard:
    """No platform-level limits (the default for the bare engine and unit tests)."""

    async def halt_reason(self) -> str | None:
        return None

    async def record(self, usage: Usage) -> None:
        return None

    async def set_killswitch(self, enabled: bool, by: str) -> None:
        raise NotImplementedError("NullGuard has no kill switch")

    async def state(self) -> dict:
        return {"killswitch": False, "spent_today_usd": 0.0, "daily_cap_usd": None}


def _day() -> str:
    return utcnow().strftime("%Y%m%d")


class InMemoryGuard:
    def __init__(self, daily_cap_usd: float) -> None:
        self._cap, self._killed, self._spend = daily_cap_usd, False, {}

    async def halt_reason(self) -> str | None:
        if self._killed:
            return "kill switch is on"
        if self._spend.get(_day(), 0.0) >= self._cap:
            return f"daily spend cap ${self._cap:.2f} reached"
        return None

    async def record(self, usage: Usage) -> None:
        self._spend[_day()] = self._spend.get(_day(), 0.0) + usage.cost_usd

    async def set_killswitch(self, enabled: bool, by: str) -> None:
        self._killed = enabled

    async def state(self) -> dict:
        return {
            "killswitch": self._killed,
            "spent_today_usd": self._spend.get(_day(), 0.0),
            "daily_cap_usd": self._cap,
        }


class FirestoreGuard:
    """Two tiny documents under `platform/`: `killswitch` and `spend-YYYYMMDD` (TTL'd)."""

    def __init__(self, client, daily_cap_usd: float, collection: str = "platform") -> None:
        self._col, self._cap = client.collection(collection), daily_cap_usd

    async def _read(self) -> tuple[bool, float]:
        ks, sp = await asyncio.gather(
            self._col.document("killswitch").get(), self._col.document(f"spend-{_day()}").get()
        )
        return bool(ks.exists and ks.to_dict().get("enabled")), float(
            sp.to_dict().get("usd", 0.0)
        ) if sp.exists else 0.0

    async def halt_reason(self) -> str | None:
        killed, spent = await self._read()
        if killed:
            return "kill switch is on"
        if spent >= self._cap:
            return f"daily spend cap ${self._cap:.2f} reached (spent ${spent:.4f})"
        return None

    async def record(self, usage: Usage) -> None:
        if usage.cost_usd <= 0:
            return
        from google.cloud import firestore

        await self._col.document(f"spend-{_day()}").set(
            {
                "usd": firestore.Increment(usage.cost_usd),
                "expires_at": utcnow() + timedelta(days=3),
            },
            merge=True,
        )

    async def set_killswitch(self, enabled: bool, by: str) -> None:
        await self._col.document("killswitch").set(
            {"enabled": enabled, "by": by, "at": utcnow().isoformat()}
        )

    async def state(self) -> dict:
        killed, spent = await self._read()
        return {"killswitch": killed, "spent_today_usd": spent, "daily_cap_usd": self._cap}
