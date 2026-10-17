"""Redis backend: vector search with the Redis Query Engine, with the permission filter inside it.

Install with `pip install "needtoknow[redis]"`. Pass a `redis.Redis` client (and, for a native
`asearch`, a `redis.asyncio.Redis`) connected to Redis 8 or later, where the Query Engine is built
in, or to Redis Stack.

Permissions are matched on one TAG field, `needtoknow_terms`, holding a type-tagged term for every
string or integer in the chunk's metadata, as the other backends do, each term hex-encoded. A
filter becomes `@needtoknow_terms:{...}` conditions on those hex strings, which contain only `0-9`
and `a-f`: no group name or user id can add query syntax, and Redis's case-insensitive TAG
matching cannot merge two values. The metadata is stored as JSON in the hash and returned exactly
as written, for the retriever's re-check.

`create_index` creates the index over the hashes under `prefix`, with an HNSW cosine vector. A
write is searchable as soon as it returns.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from needtoknow.backends._engines import _SEP, TERMS_FIELD, tag, terms
from needtoknow.backends._vectors import document_vectors
from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Hit, Query

__all__ = ["TERMS_FIELD", "RedisBackend", "create_index", "filter_to_query", "permission_terms"]

Embed = Callable[[str], Sequence[float]]

ID_FIELD = "needtoknow_id"
_SCORE = "__needtoknow_distance"
_BATCH = 500


def _hex(term: str) -> str:
    return term.encode("utf-8", "surrogatepass").hex()


def permission_terms(metadata: Mapping[str, Any]) -> str:
    """The value of `needtoknow_terms` for a chunk with this metadata, for hashes written by other tools."""
    return ",".join(_hex(t) for t in terms("metadata", metadata))


def filter_to_query(f: Filter) -> str | None:
    """Translate a filter into a Query Engine expression on the hex-encoded terms, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        if not values:
            return None
        return f"@{TERMS_FIELD}:{{{' | '.join(_hex(tag(f.field, v)) for v in values)}}}"
    if isinstance(f, And):
        parts = [filter_to_query(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else " ".join(f"({p})" for p in parts)
    if isinstance(f, Or):
        kept = [p for p in (filter_to_query(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else " | ".join(f"({p})" for p in kept)
    raise TypeError(f"not a filter: {f!r}")


def create_index(
    client: Any,
    index: str,
    dim: int,
    *,
    prefix: str = "needtoknow:",
    vector_field: str = "embedding",
    m: int = 16,
    ef_construction: int = 200,
) -> None:
    """Create the index if it does not exist: the permission terms as a TAG field and an HNSW
    cosine vector, over the hashes whose keys start with `prefix`."""
    try:
        client.execute_command(
            "FT.CREATE", index, "ON", "HASH", "PREFIX", "1", prefix,
            "SCHEMA", TERMS_FIELD, "TAG", "SEPARATOR", ",",
            vector_field, "VECTOR", "HNSW", "10", "TYPE", "FLOAT32", "DIM", str(dim),
            "DISTANCE_METRIC", "COSINE", "M", str(m), "EF_CONSTRUCTION", str(ef_construction),
        )  # fmt: skip
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogatepass")
    return value if isinstance(value, str) else None


class RedisBackend:
    """Vector search on the Redis Query Engine, with the permission filter applied inside the search.

    `embed` turns text into a vector (your embedding model). Chunks are hashes under `prefix` (the
    document id appended). `ef_runtime` is HNSW's search breadth (Redis's default when None).
    Scores are cosine similarity, 1 minus Redis's cosine distance. Pass `async_client` (a
    `redis.asyncio.Redis`) for a native `asearch`; otherwise async calls run `search` in a thread.
    Without `embed`, search with `Query(vector=...)` and pass `embeddings` to `add`.
    """

    supports_filters = True

    def __init__(
        self,
        client: Any,
        index: str,
        embed: Embed | None = None,
        *,
        prefix: str = "needtoknow:",
        vector_field: str = "embedding",
        content_field: str = "content",
        metadata_field: str = "metadata",
        ef_runtime: int | None = None,
        async_client: Any = None,
    ) -> None:
        if not index or not prefix:
            raise ValueError("index and prefix must be non-empty")
        if ef_runtime is not None and ef_runtime < 1:
            raise ValueError("ef_runtime must be positive")
        self.client = client
        self.async_client = async_client
        self.index = index
        self.embed = embed
        self.prefix = prefix
        self.vector_field = vector_field
        self.content_field = content_field
        self.metadata_field = metadata_field
        self.ef_runtime = ef_runtime

    # ---- writing -----------------------------------------------------------------------------

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        docs = list(documents)
        vectors = document_vectors(docs, embeddings, self.embed)
        for start in range(0, len(docs), _BATCH):
            pipe = self.client.pipeline(transaction=False)
            for d, v in zip(docs[start : start + _BATCH], vectors[start : start + _BATCH], strict=True):
                key = self.prefix + d.id
                pipe.delete(key)  # a replaced chunk keeps no field of its old version
                pipe.hset(
                    key,
                    mapping={
                        ID_FIELD: d.id,
                        self.content_field: d.text,
                        self.metadata_field: json.dumps(dict(d.metadata), sort_keys=True),
                        TERMS_FIELD: ",".join(_hex(t) for t in terms(d.id, d.metadata)),
                        self.vector_field: struct.pack(f"<{len(v)}f", *(float(x) for x in v)),
                    },
                )
            pipe.execute()

    def delete(self, ids: Iterable[str]) -> None:
        keys = [self.prefix + i for i in dict.fromkeys(ids)]
        for start in range(0, len(keys), _BATCH):
            self.client.delete(*keys[start : start + _BATCH])

    # ---- searching ---------------------------------------------------------------------------

    def _command(self, query: Query, k: int, f: Filter | None) -> list[Any] | None:
        where = "*"
        if f is not None:
            expression = filter_to_query(f)
            if expression is None:
                return None  # nothing can match
            where = f"({expression})"
        breadth = f" EF_RUNTIME {self.ef_runtime}" if self.ef_runtime else ""
        knn = f"{where}=>[KNN {k} @{self.vector_field} $vec{breadth} AS {_SCORE}]"
        vector = query.embedded(self.embed)
        return [
            "FT.SEARCH", self.index, knn,
            "PARAMS", "2", "vec", struct.pack(f"<{len(vector)}f", *vector),
            "SORTBY", _SCORE, "ASC",
            "RETURN", "4", ID_FIELD, self.content_field, self.metadata_field, _SCORE,
            "LIMIT", "0", str(k), "DIALECT", "2",
        ]  # fmt: skip

    def _hit(self, key: Any, fields: Mapping[str, Any]) -> Hit:
        doc_id = _text(fields.get(ID_FIELD))
        if doc_id is None:
            name = _text(key) or ""
            doc_id = name[len(self.prefix) :] if name.startswith(self.prefix) else name
        text = _text(fields.get(self.content_field))
        try:
            metadata = json.loads(_text(fields.get(self.metadata_field)) or "")
        except ValueError:
            metadata = None
        distance = _text(fields.get(_SCORE))
        return Hit(
            doc_id,
            text or "",
            1.0 - float(distance) if distance is not None else 0.0,
            # A hash without readable metadata carries no permissions: the re-check denies it.
            metadata if isinstance(metadata, dict) else {},
        )

    def _hits(self, reply: Any) -> list[Hit]:
        hits = []
        if isinstance(reply, Mapping):  # RESP3
            for result in reply.get("results", reply.get(b"results", [])):
                attributes = result.get("extra_attributes", result.get(b"extra_attributes", {}))
                fields = {_text(name) or "": value for name, value in attributes.items()}
                hits.append(self._hit(result.get("id", result.get(b"id")), fields))
        else:  # RESP2: [total, key, [field, value, ...], key, [...], ...]
            for i in range(1, len(reply) - 1, 2):
                pairs = reply[i + 1]
                fields = {_text(pairs[j]) or "": pairs[j + 1] for j in range(0, len(pairs) - 1, 2)}
                hits.append(self._hit(reply[i], fields))
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        command = self._command(Query.of(query), k, filter)
        if command is None:
            return []
        return self._hits(self.client.execute_command(*command))

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_client is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        command = await asyncio.to_thread(self._command, Query.of(query), k, filter)
        if command is None:
            return []
        return self._hits(await self.async_client.execute_command(*command))
