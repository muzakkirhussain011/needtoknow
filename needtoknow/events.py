"""Retrieval events, for audit logs and monitoring.

Every `retrieve`/`aretrieve` call, including failed ones, produces one `RetrievalEvent` and passes
it to each observer given to `PermissionedRetriever(observers=[...])`.

Events never contain the query text, the query vector or any chunk text. If the retriever is given
an `audit_key`, the event carries an HMAC-SHA256 of the query, which lets you match repeated queries
without storing them; without a key there is no fingerprint at all, because a plain hash of a short
query can be reversed by guessing.

An observer that raises is logged on the `needtoknow` logger and does not affect retrieval.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import IO, Any

from needtoknow.types import Query

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
    search_ms: float = 0.0
    """Time spent in the backend, over every search of this call."""
    authorize_ms: float = 0.0
    """Time spent in the authorizer: filters, scopes and checks."""
    searches: int = 0
    """Backend searches made: one when pre-filtering, more when post-filtering fetched again."""
    rerank_ms: float = 0.0
    """Time spent in the reranker."""
    reranked: int = 0
    """Permitted chunks given to the reranker; 0 when there is none."""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["returned_ids"] = list(self.returned_ids)
        return data


Observer = Callable[[RetrievalEvent], None]


def query_fingerprint(query: str | Query, key: bytes) -> str:
    """HMAC-SHA256 of the query under `key`, in hex. Equal queries give equal fingerprints.

    A `Query` with text is fingerprinted by its text, so the same question matches whether or not
    a vector came with it. A vector-only query is fingerprinted by its vector.
    """
    q = Query.of(query)
    if q.text is not None:
        message = q.text.encode("utf-8", "surrogatepass")  # a lone surrogate must not raise here
    else:
        assert q.vector is not None
        # 0xFF never occurs in UTF-8, so no text query has the same message as a vector.
        message = b"\xff" + struct.pack(f"<{len(q.vector)}d", *q.vector)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


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
