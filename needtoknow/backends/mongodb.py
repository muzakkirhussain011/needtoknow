"""MongoDB backend: Atlas Vector Search, with the permission filter inside `$vectorSearch`.

Install with `pip install "needtoknow[mongodb]"`. Pass a pymongo `Collection` on MongoDB Atlas or on
a local Atlas deployment (the `mongodb/mongodb-atlas-local` image) and, for a native `asearch`, the
same collection from an `AsyncMongoClient`.

Permissions are matched on one array field, `needtoknow_terms`, holding a type-tagged term for
every string or integer in the chunk's metadata, as the Elasticsearch and Milvus backends do. A
filter becomes a BSON document (`$in`, `$and`, `$or`) on that field, so values are data, never
query syntax, and `1` and `"1"` stay apart. The metadata itself is stored as written, for the
retriever's re-check. `create_search_index` creates the vector search index with the vector and
the filter fields.

Atlas Vector Search indexes a write shortly after it happens, not at once. So `add` and `delete`
wait (by default) until the index shows the change: every write carries a fresh marker,
`needtoknow_write`, and the wait ends when the index returns each chunk with its marker, which is
also when its new permission terms are searchable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from needtoknow.backends._engines import _SEP, TERMS_FIELD, tag, terms
from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Hit

__all__ = [
    "TERMS_FIELD",
    "WRITE_FIELD",
    "MongoDBBackend",
    "create_search_index",
    "filter_to_mongo",
    "permission_terms",
]

Embed = Callable[[str], Sequence[float]]

WRITE_FIELD = "needtoknow_write"
"""A marker each `add` writes, so it can tell when the search index has caught up."""

_BATCH = 500
_MAX_CANDIDATES = 10_000  # Atlas Vector Search's limit for numCandidates (and so for limit)


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `needtoknow_terms` for a chunk with this metadata, for documents written by other tools."""
    return terms("metadata", metadata)


def filter_to_mongo(f: Filter) -> dict[str, Any] | None:
    """Translate a filter into a `$vectorSearch` filter on the tagged terms, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        return {TERMS_FIELD: {"$in": [tag(f.field, v) for v in values]}} if values else None
    if isinstance(f, And):
        parts = [filter_to_mongo(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else {"$and": parts}
    if isinstance(f, Or):
        kept = [p for p in (filter_to_mongo(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else {"$or": kept}
    raise TypeError(f"not a filter: {f!r}")


def create_search_index(
    collection: Any,
    dim: int,
    *,
    name: str = "needtoknow",
    vector_field: str = "embedding",
    similarity: str = "cosine",
    wait: bool = True,
    timeout: float = 120.0,
) -> None:
    """Create the vector search index `MongoDBBackend` uses, creating the collection if needed.

    The index holds the vector and, as filter fields, the permission terms, the write marker and
    `_id`. With `wait`, it returns once the index is queryable, or raises `TimeoutError`.
    """
    from pymongo.operations import SearchIndexModel

    database = collection.database
    if collection.name not in database.list_collection_names():
        database.create_collection(collection.name)
    definition = {
        "fields": [
            {"type": "vector", "path": vector_field, "numDimensions": dim, "similarity": similarity},
            {"type": "filter", "path": TERMS_FIELD},
            {"type": "filter", "path": WRITE_FIELD},
            {"type": "filter", "path": "_id"},
        ]
    }
    collection.create_search_index(SearchIndexModel(definition=definition, name=name, type="vectorSearch"))
    if not wait:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(index.get("queryable") for index in collection.list_search_indexes(name)):
            return
        time.sleep(0.5)
    raise TimeoutError(f"search index {name!r} was not queryable after {timeout} seconds")


class MongoDBBackend:
    """Vector search on MongoDB Atlas Vector Search, with the permission filter applied inside the search.

    `embed` turns text into a vector (your embedding model). `index` names the vector search index.
    `num_candidates` is how many neighbours the approximate search considers (raised to `k`, at
    most 10,000): higher finds more of the true nearest chunks and takes longer. `exact=True`
    searches every chunk instead. `wait_timeout` bounds how long `add` and `delete` wait for the
    index. Pass `async_collection` (the same collection from an `AsyncMongoClient`) for a native
    `asearch`; otherwise async calls run `search` in a thread.
    """

    supports_filters = True

    def __init__(
        self,
        collection: Any,
        embed: Embed,
        *,
        index: str = "needtoknow",
        vector_field: str = "embedding",
        content_field: str = "content",
        metadata_field: str = "metadata",
        num_candidates: int = 100,
        exact: bool = False,
        wait_timeout: float = 60.0,
        async_collection: Any = None,
    ) -> None:
        if not index:
            raise ValueError("index must be a non-empty name")
        if num_candidates < 1 or wait_timeout <= 0:
            raise ValueError("num_candidates and wait_timeout must be positive")
        self.collection = collection
        self.async_collection = async_collection
        self.embed = embed
        self.index = index
        self.vector_field = vector_field
        self.content_field = content_field
        self.metadata_field = metadata_field
        self.num_candidates = num_candidates
        self.exact = exact
        self.wait_timeout = wait_timeout

    # ---- writing -----------------------------------------------------------------------------

    def _document(self, doc: Document, vector: Sequence[float], marker: str) -> dict[str, Any]:
        return {
            "_id": doc.id,
            self.content_field: doc.text,
            self.metadata_field: dict(doc.metadata),
            TERMS_FIELD: terms(doc.id, doc.metadata),
            self.vector_field: [float(x) for x in vector],
            WRITE_FIELD: marker,
        }

    def add(
        self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None, *, wait: bool = True
    ) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given.

        With `wait` (the default), returns once the search index shows every chunk as written,
        or raises `TimeoutError` after `wait_timeout` seconds.
        """
        from pymongo import ReplaceOne

        docs = list(documents)
        vectors = list(embeddings) if embeddings is not None else [self.embed(d.text) for d in docs]
        if len(vectors) != len(docs):
            raise ValueError("one embedding per document is required")
        # The last version of each id wins, as it would in sequence; each id is then written once,
        # so every batch's marker stays on all of its chunks.
        latest = list({d.id: (d, v) for d, v in zip(docs, vectors, strict=True)}.values())
        batches: list[tuple[str, int]] = []
        for start in range(0, len(latest), _BATCH):
            marker = uuid.uuid4().hex
            rows = [self._document(d, v, marker) for d, v in latest[start : start + _BATCH]]
            self.collection.bulk_write([ReplaceOne({"_id": r["_id"]}, r, upsert=True) for r in rows], ordered=False)
            batches.append((marker, len(rows)))
        if wait and latest:
            probe = [float(x) for x in latest[0][1]]
            for marker, expected in batches:
                self._wait({WRITE_FIELD: marker}, expected, probe)

    def delete(self, ids: Iterable[str], *, wait: bool = True) -> None:
        """Delete chunks by id; with `wait`, returns once the search index no longer returns them."""
        selected = list(dict.fromkeys(ids))
        if not selected:
            return
        self.collection.delete_many({"_id": {"$in": selected}})
        if wait:
            probe = [float(x) for x in self.embed("needtoknow")]
            for start in range(0, len(selected), _MAX_CANDIDATES):
                self._wait({"_id": {"$in": selected[start : start + _MAX_CANDIDATES]}}, 0, probe)

    def _count(self, flt: dict[str, Any], probe: list[float]) -> int:
        """How many chunks matching `flt` the search index returns, by an exact search."""
        stage: dict[str, Any] = {
            "index": self.index,
            "path": self.vector_field,
            "queryVector": probe,
            "exact": True,
            "limit": _MAX_CANDIDATES,
            "filter": flt,
        }
        return len(list(self.collection.aggregate([{"$vectorSearch": stage}, {"$project": {"_id": 1}}])))

    def _wait(self, flt: dict[str, Any], expected: int, probe: list[float]) -> None:
        deadline = time.monotonic() + self.wait_timeout
        while True:
            if self._count(flt, probe) == expected:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"the search index did not catch up within {self.wait_timeout} seconds")
            time.sleep(0.2)

    # ---- searching ---------------------------------------------------------------------------

    def _pipeline(self, query: str, k: int, f: Filter | None) -> list[dict[str, Any]] | None:
        stage: dict[str, Any] = {
            "index": self.index,
            "path": self.vector_field,
            "queryVector": [float(x) for x in self.embed(query)],
            "limit": min(k, _MAX_CANDIDATES),
        }
        if self.exact:
            stage["exact"] = True
        else:
            stage["numCandidates"] = min(max(self.num_candidates, k), _MAX_CANDIDATES)
        if f is not None:
            mongo_filter = filter_to_mongo(f)
            if mongo_filter is None:
                return None  # nothing can match
            stage["filter"] = mongo_filter
        projection = {self.content_field: 1, self.metadata_field: 1, "score": {"$meta": "vectorSearchScore"}}
        return [{"$vectorSearch": stage}, {"$project": projection}]

    def _hits(self, documents: Iterable[Mapping[str, Any]]) -> list[Hit]:
        hits = []
        for d in documents:
            text = d.get(self.content_field)
            metadata = d.get(self.metadata_field)
            hits.append(
                Hit(
                    str(d.get("_id")),
                    text if isinstance(text, str) else "",
                    float(d.get("score", 0.0)),
                    # A document without a metadata object carries no permissions: the re-check denies it.
                    dict(metadata) if isinstance(metadata, Mapping) else {},
                )
            )
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        pipeline = self._pipeline(query, k, filter)
        if pipeline is None:
            return []
        return self._hits(self.collection.aggregate(pipeline))

    async def asearch(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_collection is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        pipeline = await asyncio.to_thread(self._pipeline, query, k, filter)
        if pipeline is None:
            return []
        cursor = await self.async_collection.aggregate(pipeline)
        return self._hits(await cursor.to_list(None))
