"""Retrieval events, for audit logs and monitoring.

Every `retrieve`/`aretrieve` call, including failed ones, produces one `RetrievalEvent` and passes
it to each observer given to `PermissionedRetriever(observers=[...])`.

Events never contain the query text or any chunk text. If the retriever is given an `audit_key`,
the event carries an HMAC-SHA256 of the query, which lets you match repeated queries without
storing them; without a key there is no fingerprint at all, because a plain hash of a short query
can be reversed by guessing.

An observer that raises is logged on the `needtoknow` logger and does not affect retrieval.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import IO, Any

__all__ = ["JsonLinesAudit", "Observer", "RetrievalEvent", "query_fingerprint"]


@dataclass(frozen=True)
class RetrievalEvent:
    """One retrieval, for audit logs and monitoring. Never contains query or chunk text."""

    subject_id: str
    action: str
    k: int
    strategy: str | None
    """The strategy that ran, or None if the call failed before choosing one."""
    returned_ids: tuple[str, ...] = ()
    examined: int = 0
    denied: int = 0
    violations: int = 0
    complete: bool = False
    duration_ms: float = 0.0
    error: str | None = None
    """The exception type when the call failed, for example "AuthorizationError"."""
    query_hmac: str | None = None
    started_at: float = 0.0
    """Unix time when the call started."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["returned_ids"] = list(self.returned_ids)
        return data


Observer = Callable[[RetrievalEvent], None]


def query_fingerprint(query: str, key: bytes) -> str:
    """HMAC-SHA256 of the query under `key`, in hex. Equal queries give equal fingerprints."""
    return hmac.new(key, query.encode("utf-8"), hashlib.sha256).hexdigest()


class JsonLinesAudit:
    """Writes one JSON object per retrieval to a text stream (a file, stdout, a socket wrapper).

    It records who asked, what came back and what was denied, never the query or chunk text.
    """

    def __init__(self, stream: IO[str]) -> None:
        self.stream = stream
        self._lock = threading.Lock()

    def __call__(self, event: RetrievalEvent) -> None:
        line = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()
