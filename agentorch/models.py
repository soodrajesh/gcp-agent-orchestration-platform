"""Data model for a durable run. Everything here is JSON-serialisable so any store can hold it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    BUDGET_EXCEEDED = "budget_exceeded"
    HALTED = "halted"  # platform kill switch or daily cap, see guard.py

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.REJECTED,
            self.BUDGET_EXCEEDED,
            self.HALTED,
        }


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class Budget(BaseModel):
    max_tokens: int = 200_000
    max_cost_usd: float = 0.25


class StepRecord(BaseModel):
    """A *completed* step. Its presence is the checkpoint: a done step is never re-executed."""

    key: str
    output: Any = None
    usage: Usage = Field(default_factory=Usage)
    finished_at: datetime = Field(default_factory=utcnow)


class Approval(BaseModel):
    step_key: str
    prompt: str
    requested_at: datetime = Field(default_factory=utcnow)
    decided: bool = False
    approved: bool = False
    decided_by: str | None = None
    decided_at: datetime | None = None


class Run(BaseModel):
    id: str
    workflow: str
    input: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.PENDING
    step_order: list[str] = Field(default_factory=list)  # lets viewers render pending steps
    budget: Budget = Field(default_factory=Budget)
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    approvals: dict[str, Approval] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    lease_owner: str | None = None
    lease_expires: datetime | None = None
    version: int = 0  # optimistic-concurrency counter; the store bumps it on every save
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    # Firestore TTL deletes the document after this instant (keeps the free 1 GiB empty).
    expires_at: datetime = Field(default_factory=lambda: utcnow() + timedelta(days=30))
