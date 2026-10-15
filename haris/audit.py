"""Tamper-evident audit log.

Every record is hashed together with the previous record's hash (a hash chain), so editing or
deleting any past record breaks verification from that point on. In PostgreSQL the table also has
a trigger that rejects UPDATE and DELETE, so the log is append-only at the database level too.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

GENESIS_HASH = "0" * 64


def canonical_json(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def chain_hash(prev_hash: str, record_json: str) -> str:
    return hashlib.sha256((prev_hash + record_json).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    record: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class ChainStatus:
    ok: bool
    checked: int
    first_bad_seq: int | None = None


class AuditLog(Protocol):
    def append(self, record: dict[str, Any]) -> AuditEntry: ...

    def recent(self, limit: int) -> list[AuditEntry]: ...

    def verify(self) -> ChainStatus: ...


def _verify(entries: list[tuple[int, str, str, str]]) -> ChainStatus:
    """entries: (seq, record_json, prev_hash, hash) in ascending seq order."""
    prev = GENESIS_HASH
    for seq, record_json, prev_hash, stored in entries:
        if prev_hash != prev or chain_hash(prev, record_json) != stored:
            return ChainStatus(ok=False, checked=seq, first_bad_seq=seq)
        prev = stored
    return ChainStatus(ok=True, checked=len(entries))


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._rows: list[tuple[int, str, str, str]] = []
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> AuditEntry:
        record_json = canonical_json(record)
        with self._lock:
            prev = self._rows[-1][3] if self._rows else GENESIS_HASH
            seq = len(self._rows) + 1
            h = chain_hash(prev, record_json)
            self._rows.append((seq, record_json, prev, h))
        return AuditEntry(seq, json.loads(record_json), prev, h)

    def recent(self, limit: int) -> list[AuditEntry]:
        return [AuditEntry(s, json.loads(r), p, h) for s, r, p, h in reversed(self._rows[-limit:])]

    def verify(self) -> ChainStatus:
        with self._lock:
            return _verify(list(self._rows))

    # test hook: simulate someone editing history
    def _tamper(self, seq: int, record: dict[str, Any]) -> None:
        s, _, p, h = self._rows[seq - 1]
        self._rows[seq - 1] = (s, canonical_json(record), p, h)


class PostgresAuditLog:
    _LOCK_KEY = 727_001  # advisory lock that serialises appends so the chain stays linear

    def __init__(self, pool) -> None:
        self._pool = pool

    def append(self, record: dict[str, Any]) -> AuditEntry:
        record_json = canonical_json(record)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._LOCK_KEY,))
            row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS_HASH
            h = chain_hash(prev, record_json)
            seq = conn.execute(
                "INSERT INTO audit_log (record_json, prev_hash, hash) VALUES (%s, %s, %s) RETURNING seq",
                (record_json, prev, h),
            ).fetchone()[0]
        return AuditEntry(int(seq), json.loads(record_json), prev, h)

    def recent(self, limit: int) -> list[AuditEntry]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT seq, record_json, prev_hash, hash FROM audit_log ORDER BY seq DESC LIMIT %s", (limit,)
            ).fetchall()
        return [AuditEntry(int(s), json.loads(r), p, h) for s, r, p, h in rows]

    def verify(self) -> ChainStatus:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT seq, record_json, prev_hash, hash FROM audit_log ORDER BY seq").fetchall()
        return _verify([(int(s), r, p, h) for s, r, p, h in rows])
