"""Qdrant backend: the permission filter becomes Qdrant's `query_filter`.

Install with `pip install "needtoknow[qdrant]"`. Pass a `QdrantClient` (a server, Qdrant Cloud, or
`QdrantClient(":memory:")` for tests) and, for a native `asearch`, an `AsyncQdrantClient`.

Points are stored the way LangChain's Qdrant integration stores them: the text under
`page_content` and the metadata under `metadata`, so collections built with LangChain can be searched
with permissions as they are. Filters address `metadata.<field>`; for list fields Qdrant matches when
any element matches, which is the reference semantics of `In`. Field names used in filters may not
contain `.`, `[`, `]` or `"`, which Qdrant reads as path syntax.

Qdrant point ids are unsigned integers or UUIDs. `add` derives a UUID from each document id (or uses
the id itself when it is a UUID) and keeps the original id in the payload under `needtoknow_id`;
search results report that id, or the point id for points written by something else.

Create the collection with cosine distance, and index the permission fields: `create_collection`
does both. Without a payload index Qdrant still filters correctly, but slowly on large collections.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from needtoknow.backends._vectors import document_vectors
from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Hit, Query

if TYPE_CHECKING:
    from qdrant_client import models

__all__ = ["ID_KEY", "QdrantBackend", "create_collection", "filter_to_qdrant", "point_id"]

Embed = Callable[[str], Sequence[float]]

ID_KEY = "needtoknow_id"
"""Payload key holding the document id that `add` was given."""

_NAMESPACE = uuid.UUID("5e3b1d7c-6b7a-4f0e-9a51-6f1d0c2b8e44")
_PATH_SYNTAX = frozenset('.[]"')
_BATCH = 256


def point_id(document_id: str) -> str:
    """The Qdrant point id for a document id: the id itself if it is a UUID, otherwise a UUID derived from it."""
    try:
        return str(uuid.UUID(document_id))
    except ValueError:
        return str(uuid.uuid5(_NAMESPACE, document_id))


def _key(metadata_key: str, field: str) -> str:
    if not field or _PATH_SYNTAX & set(field):
        raise ValueError(f"field {field!r} cannot be filtered in Qdrant: '.', '[', ']' and '\"' are path syntax")
    return f"{metadata_key}.{field}"


def filter_to_qdrant(f: Filter, metadata_key: str = "metadata") -> models.Filter | None:
    """Translate a filter into a Qdrant `Filter`, or `None` when nothing can match.

    `In` becomes `MatchAny` on `metadata.<field>`, one condition per value type (strings and
    integers are never mixed in one list), joined with `should`. An `In` with no values matches
    nothing; that is decided here instead of being left to Qdrant's handling of empty lists.
    """
    from qdrant_client import models

    def condition(f: Filter) -> Any:
        if isinstance(f, In):
            key = _key(metadata_key, f.field)
            values = _ordered(f.values)
            strings = [v for v in values if isinstance(v, str)]
            ints = [v for v in values if isinstance(v, int)]
            parts: list[Any] = []
            if strings:
                parts.append(models.FieldCondition(key=key, match=models.MatchAny(any=strings)))
            if ints:
                parts.append(models.FieldCondition(key=key, match=models.MatchAny(any=ints)))
            if not parts:
                return None
            return parts[0] if len(parts) == 1 else models.Filter(should=parts)
        if isinstance(f, And):
            parts = [condition(c) for c in f.clauses]
            return None if any(p is None for p in parts) else models.Filter(must=parts)
        if isinstance(f, Or):
            parts = [p for p in (condition(c) for c in f.clauses) if p is not None]
            return models.Filter(should=parts) if parts else None
        raise TypeError(f"not a filter: {f!r}")

    result = condition(f)
    if result is None or isinstance(result, models.Filter):
        return result
    return models.Filter(must=[result])


def create_collection(
    client: Any,
    name: str,
    dim: int,
    *,
    keyword_fields: Sequence[str] = ("allowed_users", "allowed_groups"),
    metadata_key: str = "metadata",
    vector_name: str | None = None,
) -> None:
    """Create a cosine-distance collection if it does not exist, and keyword indexes on the
    permission fields (`metadata.allowed_users` and `metadata.allowed_groups` by default)."""
    from qdrant_client import models

    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    if not client.collection_exists(name):
        params = models.VectorParams(size=dim, distance=models.Distance.COSINE)
        client.create_collection(name, vectors_config=params if vector_name is None else {vector_name: params})
    for field in keyword_fields:
        client.create_payload_index(name, _key(metadata_key, field), field_schema=models.PayloadSchemaType.KEYWORD)


class QdrantBackend:
    """Search a Qdrant collection with the permission filter applied by Qdrant.

    `embed` turns text into a vector (your embedding model). `vector_name` selects a named vector,
    `search_params` (a `qdrant_client.models.SearchParams`) tunes the search, for example
    `SearchParams(hnsw_ef=128)` for higher recall or `SearchParams(exact=True)` for brute force.
    Scores are Qdrant's: cosine similarity for a cosine collection. Pass `async_client` (an
    `AsyncQdrantClient`) for a native `asearch`; otherwise async calls run `search` in a thread.
    Without `embed`, search with `Query(vector=...)` and pass `embeddings` to `add`.
    """

    supports_filters = True

    def __init__(
        self,
        client: Any,
        collection: str,
        embed: Embed | None = None,
        *,
        content_key: str = "page_content",
        metadata_key: str = "metadata",
        vector_name: str | None = None,
        search_params: Any = None,
        async_client: Any = None,
    ) -> None:
        if not collection:
            raise ValueError("collection must be a non-empty name")
        self.client = client
        self.async_client = async_client
        self.collection = collection
        self.embed = embed
        self.content_key = content_key
        self.metadata_key = metadata_key
        self.vector_name = vector_name
        self.search_params = search_params

    # ---- writing -----------------------------------------------------------------------------

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        from qdrant_client import models

        docs = list(documents)
        vectors = document_vectors(docs, embeddings, self.embed)
        points = [
            models.PointStruct(
                id=point_id(d.id),
                vector=[float(x) for x in v] if self.vector_name is None else {self.vector_name: [float(x) for x in v]},
                payload={ID_KEY: d.id, self.content_key: d.text, self.metadata_key: dict(d.metadata)},
            )
            for d, v in zip(docs, vectors, strict=True)
        ]
        for start in range(0, len(points), _BATCH):
            self.client.upsert(self.collection, points=points[start : start + _BATCH], wait=True)

    def delete(self, ids: Iterable[str]) -> None:
        from qdrant_client import models

        selected: list[int | str | uuid.UUID] = [point_id(i) for i in ids]
        if selected:
            self.client.delete(self.collection, points_selector=models.PointIdsList(points=selected), wait=True)

    # ---- searching ---------------------------------------------------------------------------

    def _request(self, query: Query, k: int, f: Filter | None) -> dict[str, Any] | None:
        query_filter = None
        if f is not None:
            query_filter = filter_to_qdrant(f, self.metadata_key)
            if query_filter is None:
                return None  # nothing can match
        return {
            "collection_name": self.collection,
            "query": query.embedded(self.embed),
            "using": self.vector_name,
            "query_filter": query_filter,
            "search_params": self.search_params,
            "limit": k,
            "with_payload": True,
        }

    def _hits(self, points: Iterable[Any]) -> list[Hit]:
        hits = []
        for p in points:
            payload = p.payload or {}
            doc_id = payload.get(ID_KEY)
            text = payload.get(self.content_key)
            metadata = payload.get(self.metadata_key)
            hits.append(
                Hit(
                    doc_id if isinstance(doc_id, str) else str(p.id),
                    text if isinstance(text, str) else "",
                    float(p.score),
                    # A point without a metadata object carries no permissions: the re-check denies it.
                    metadata if isinstance(metadata, dict) else {},
                )
            )
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        request = self._request(Query.of(query), k, filter)
        if request is None:
            return []
        return self._hits(self.client.query_points(**request).points)

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_client is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        request = await asyncio.to_thread(self._request, Query.of(query), k, filter)
        if request is None:
            return []
        response = await self.async_client.query_points(**request)
        return self._hits(response.points)
