"""
Token and cost accounting. After every successful call the gateway writes one
UsageRecord: who called, which model answered, the tokens and what they cost.
Budgets and usage reports are built on top of these records.

Stores are pluggable: SQLiteUsageStore is the default and fine for a single
gateway; several gateways sharing one database need a networked store (a
Postgres one would be a welcome contribution: implement UsageStore).
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from llmops.providers import Usage


class Price(BaseModel):
    """USD per million tokens, the way providers list their prices."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)

    def cost(self, usage: Usage) -> float:
        return (usage.prompt_tokens * self.input + usage.completion_tokens * self.output) / 1_000_000


class UsageRecord(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    team: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    request_id: str | None = None


class UsageRow(BaseModel):
    """Totals for one team and model."""

    team: str
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class UsageStore(ABC):
    @abstractmethod
    def record(self, rec: UsageRecord) -> None: ...

    @abstractmethod
    def summary(
        self, since: datetime, until: datetime | None = None, team: str | None = None
    ) -> list[UsageRow]:
        """Totals per (team, model) for records with since <= ts < until, sorted by team then model."""

    def spend(self, team: str, since: datetime) -> float:
        return sum(row.cost_usd for row in self.summary(since, team=team))

    def close(self) -> None:
        return None


class MemoryUsageStore(UsageStore):
    """Keeps records in a list. For tests, and for running without a database file."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, rec: UsageRecord) -> None:
        self.records.append(rec)

    def summary(
        self, since: datetime, until: datetime | None = None, team: str | None = None
    ) -> list[UsageRow]:
        rows: dict[tuple[str, str], UsageRow] = {}
        for r in self.records:
            if r.ts < since or (until is not None and r.ts >= until) or (team and r.team != team):
                continue
            row = rows.setdefault(
                (r.team, r.model),
                UsageRow(
                    team=r.team, model=r.model, requests=0, prompt_tokens=0, completion_tokens=0, cost_usd=0
                ),
            )
            row.requests += 1
            row.prompt_tokens += r.prompt_tokens
            row.completion_tokens += r.completion_tokens
            row.cost_usd += r.cost_usd
        return [rows[k] for k in sorted(rows)]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    ts TEXT NOT NULL,              -- ISO 8601 in UTC, so text order is time order
    team TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS usage_team_ts ON usage (team, ts);
"""


def _utc_text(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(timezone.utc).isoformat()


class SQLiteUsageStore(UsageStore):
    def __init__(self, path: str | Path):
        # FastAPI runs sync code in a thread pool, so one connection is shared behind a lock
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def record(self, rec: UsageRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utc_text(rec.ts),
                    rec.team,
                    rec.model,
                    rec.provider,
                    rec.prompt_tokens,
                    rec.completion_tokens,
                    rec.cost_usd,
                    rec.request_id,
                ),
            )

    def summary(
        self, since: datetime, until: datetime | None = None, team: str | None = None
    ) -> list[UsageRow]:
        query = (
            "SELECT team, model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_usd)"
            " FROM usage WHERE ts >= ?"
        )
        params: list[str] = [_utc_text(since)]
        if until is not None:
            query += " AND ts < ?"
            params.append(_utc_text(until))
        if team is not None:
            query += " AND team = ?"
            params.append(team)
        query += " GROUP BY team, model ORDER BY team, model"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            UsageRow(team=t, model=m, requests=n, prompt_tokens=p, completion_tokens=c, cost_usd=cost)
            for t, m, n, p, c, cost in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def open_store(spec: str) -> UsageStore:
    """`memory` for an in-memory store, anything else is a SQLite file path."""
    if spec == "memory":
        return MemoryUsageStore()
    return SQLiteUsageStore(spec)
