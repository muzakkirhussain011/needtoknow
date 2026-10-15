"""Per-team daily token usage, used by the policy to enforce budgets."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from typing import Protocol


def today() -> date:
    return datetime.now(UTC).date()


class UsageStore(Protocol):
    def tokens_today(self, team: str) -> int: ...

    def add(self, team: str, tokens: int) -> None: ...


class InMemoryUsageStore:
    def __init__(self) -> None:
        self._tokens: dict[tuple[str, date], int] = {}
        self._lock = threading.Lock()

    def tokens_today(self, team: str) -> int:
        return self._tokens.get((team, today()), 0)

    def add(self, team: str, tokens: int) -> None:
        with self._lock:
            key = (team, today())
            self._tokens[key] = self._tokens.get(key, 0) + tokens


class PostgresUsageStore:
    def __init__(self, pool) -> None:
        self._pool = pool

    def tokens_today(self, team: str) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT tokens FROM usage_daily WHERE team = %s AND day = %s", (team, today())
            ).fetchone()
        return int(row[0]) if row else 0

    def add(self, team: str, tokens: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO usage_daily (team, day, tokens, requests) VALUES (%s, %s, %s, 1)
                ON CONFLICT (team, day) DO UPDATE
                  SET tokens = usage_daily.tokens + EXCLUDED.tokens, requests = usage_daily.requests + 1
                """,
                (team, today(), tokens),
            )
