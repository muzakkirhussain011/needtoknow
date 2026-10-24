"""Pinecone backend: dense vector search on a Pinecone index, with the permission filter in the query.

Talks to the index's data plane over HTTP (`/vectors/upsert`, `/query`, `/vectors/update`,
`/vectors/delete`) with httpx, the `pinecone` extra. The index must be a vector index (created
with a dimension and metric, or the `_values` field), not a document index.

Each chunk is a record whose metadata holds its id, its text, its metadata as JSON (returned exactly
as written), and the tagged permission terms of `wardowl.backends._engines`, hex-encoded, as a
list of strings. A filter becomes `$in` conditions on those terms joined with `$and` and `$or`,
which Pinecone applies inside the search, so pre-filtering is exact; the terms keep `1` and `"1"`
apart. Pinecone record ids are ASCII of at most 512 characters, so other chunk ids are stored under
their SHA-256.

Pinecone's serverless indexes are eventually consistent. `wait=30.0` makes every write wait, up to
that many seconds, until a search sees it; without it a write may take a few seconds to show.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from wardowl.backends._engines import _SEP, tag, terms
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Embed, Hit, Query

__all__ = ["TERMS_FIELD", "PineconeBackend", "filter_to_pinecone", "record_id"]

TERMS_FIELD = "wardowl_terms"
ID_FIELD = "wardowl_id"
METADATA_FIELD = "wardowl_metadata"
TEXT_FIELD = "wardowl_text"
API_VERSION = "2026-07"
_MAX_IN = 10_000  # Pinecone's limit on the values of one $in
_MAX_TOP_K = 10_000
_BATCH = 100


def _hex(term: str) -> str:
    return term.encode().hex()


def record_id(doc_id: str) -> str:
    """The Pinecone record id for a chunk id: itself when it is printable ASCII of 1 to 512 characters."""
    if doc_id and len(doc_id) <= 512 and doc_id.isascii() and doc_id.isprintable():
        return doc_id
    return "sha256-" + hashlib.sha256(doc_id.encode()).hexdigest()


def filter_to_pinecone(f: Filter) -> dict[str, Any] | None:
    """Translate a filter into a Pinecone metadata filter on the hex-encoded terms, or None when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = [_hex(tag(f.field, v)) for v in _ordered(f.values)]
        if not values:
            return None
        parts = [{TERMS_FIELD: {"$in": values[i : i + _MAX_IN]}} for i in range(0, len(values), _MAX_IN)]
        return parts[0] if len(parts) == 1 else {"$or": parts}
    if isinstance(f, And):
        found = [filter_to_pinecone(c) for c in f.clauses]
        if any(p is None for p in found):
            return None
        return found[0] if len(found) == 1 else {"$and": found}
    if isinstance(f, Or):
        kept = [p for p in (filter_to_pinecone(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else {"$or": kept}
    raise TypeError(f"not a filter: {f!r}")


def _httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised without the extra installed
        raise ImportError('PineconeBackend needs httpx: pip install "wardowl[pinecone]"') from exc
    return httpx


def _settled(seen: Mapping[Any, Any], doc_id: str, want: str | None, only_stored: bool) -> bool:
    if want is None:  # deleted
        return doc_id not in seen
    return seen.get(doc_id) == want or (only_stored and doc_id not in seen)


class PineconeBackend:
    """Chunks in a Pinecone index, searched with the permission filter in the query.

    `host` is the index's host (from the console, or `describe_index`), and `api_key` a Pinecone API
    key, kept out of every message and representation. `namespace` keeps tenants or corpora apart.
    Pass `client`, an `httpx.Client`, for proxies or custom TLS. Thread-safe.
    """

    supports_filters = True

    def __init__(
        self,
        host: str,
        api_key: str,
        embed: Embed | None,
        *,
        namespace: str = "",
        wait: float | None = None,
        client: Any = None,
        timeout: float = 10.0,
        concurrency: int = 8,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        self.url = (host if host.startswith(("https://", "http://")) else f"https://{host}").rstrip("/")
        self.embed = embed
        self.namespace = namespace
        self.wait = wait
        self.concurrency = concurrency
        self._dim: int | None = None
        self._headers = {"Api-Key": api_key, "X-Pinecone-Api-Version": API_VERSION}
        self._owns_client = client is None
        self._client = client if client is not None else _httpx().Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"PineconeBackend({self.url!r}, namespace={self.namespace!r})"

    def close(self) -> None:
        """Close the HTTP client, if the backend created it."""
        if self._owns_client:
            self._client.close()

    def _post(self, path: str, body: dict[str, Any], what: str, ok: frozenset[int] = frozenset({200})) -> Any:
        if self.namespace and path != "/describe_index_stats":
            body = {**body, "namespace": self.namespace}
        response = self._client.post(f"{self.url}{path}", json=body, headers=self._headers)
        if response.status_code not in ok:
            raise RuntimeError(f"Pinecone {what} failed: HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.status_code == 200 else {}

    def _each(self, items: Sequence[Any], send: Any) -> None:
        if len(items) <= 1:
            for item in items:
                send(item)
            return
        with ThreadPoolExecutor(min(self.concurrency, len(items))) as pool:
            list(pool.map(send, items))

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Store chunks, replacing any with the same id. `embeddings` gives their vectors, in order."""
        docs = list(documents)
        if not docs:
            return
        vectors = document_vectors(docs, embeddings, self.embed)
        self._dim = len(vectors[0])
        latest = {d.id: i for i, d in enumerate(docs)}  # the last of repeated ids wins, as in order
        records: list[dict[str, Any]] = []
        expected: dict[str, str | None] = {}
        for i in latest.values():
            doc = docs[i]
            metadata: dict[str, Any] = {
                ID_FIELD: doc.id,
                TEXT_FIELD: doc.text,
                METADATA_FIELD: json.dumps(dict(doc.metadata)),
                TERMS_FIELD: [_hex(t) for t in terms(doc.id, doc.metadata)],
            }
            records.append({"id": record_id(doc.id), "values": [float(x) for x in vectors[i]], "metadata": metadata})
            expected[doc.id] = metadata[METADATA_FIELD]
        batches = [records[i : i + _BATCH] for i in range(0, len(records), _BATCH)]
        self._each(batches, lambda batch: self._post("/vectors/upsert", {"vectors": batch}, "upsert"))
        self._settle(expected)

    def delete(self, ids: Iterable[str]) -> None:
        """Remove chunks by id. Ids not stored are ignored."""
        unique = sorted(set(ids))
        for i in range(0, len(unique), 1000):
            self._post("/vectors/delete", {"ids": [record_id(d) for d in unique[i : i + 1000]]}, "delete")
        self._settle(dict.fromkeys(unique))

    def clear(self) -> None:
        """Remove every chunk in the backend's namespace."""
        self._post("/vectors/delete", {"deleteAll": True}, "delete", frozenset({200, 404}))  # 404: already empty

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored."""
        updates: list[dict[str, Any]] = []
        expected: dict[str, str | None] = {}
        for doc_id, new in metadata.items():
            fields: dict[str, Any] = {
                METADATA_FIELD: json.dumps(dict(new)),
                TERMS_FIELD: [_hex(t) for t in terms(doc_id, new)],
            }
            updates.append({"id": record_id(doc_id), "setMetadata": fields})
            expected[doc_id] = fields[METADATA_FIELD]
        self._each(updates, lambda u: self._post("/vectors/update", u, "update"))
        self._settle(expected, only_stored=True)

    def _query(self, vector: Sequence[float], k: int, where: dict[str, Any] | None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"vector": [float(x) for x in vector], "topK": k, "includeMetadata": True}
        if where is not None:
            body["filter"] = where
        matches = self._post("/query", body, "query").get("matches")
        if not isinstance(matches, list):
            raise RuntimeError("Pinecone query failed: the answer has no matches")
        return matches

    def _settle(self, expected: Mapping[str, str | None], *, only_stored: bool = False) -> None:
        """With `wait`, poll until a search shows each chunk with its metadata (None: deleted)."""
        if self.wait is None or not expected:
            return
        ids = list(expected)
        deadline = time.monotonic() + self.wait
        pause = 0.25
        probe = [1.0] + [0.0] * (self._dimension() - 1)  # any vector: the filter picks the records
        while True:
            seen: dict[Any, Any] = {}
            for i in range(0, len(ids), 1000):
                where = {ID_FIELD: {"$in": ids[i : i + 1000]}}
                for match in self._query(probe, min(len(ids) - i, 1000), where):
                    meta = match.get("metadata") or {}
                    seen[meta.get(ID_FIELD)] = meta.get(METADATA_FIELD)
            if all(_settled(seen, doc_id, want, only_stored) for doc_id, want in expected.items()):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Pinecone did not show the writes to a search within {self.wait} seconds")
            time.sleep(pause)
            pause = min(pause * 2, 4.0)

    def _dimension(self) -> int:
        if self._dim is None:
            self._dim = int(self._post("/describe_index_stats", {}, "describe_index_stats")["dimension"])
        return self._dim

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        where = None
        if filter is not None:
            where = filter_to_pinecone(filter)
            if where is None:
                return []
        vector = Query.of(query).embedded(self.embed)
        hits = []
        for match in self._query(vector, min(k, _MAX_TOP_K), where):
            meta = match.get("metadata") or {}
            if not isinstance(meta.get(ID_FIELD), str):
                raise RuntimeError(f"Pinecone returned a record without {ID_FIELD}: {match.get('id')!r}")
            metadata = json.loads(meta.get(METADATA_FIELD) or "{}")
            hits.append(Hit(meta[ID_FIELD], meta.get(TEXT_FIELD, ""), float(match["score"]), metadata))
        return hits
