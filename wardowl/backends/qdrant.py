"""Qdrant backend: the permission filter becomes Qdrant's `query_filter`.

Install with `pip install "wardowl[qdrant]"`. Pass a `QdrantClient` (a server, Qdrant Cloud, or
`QdrantClient(":memory:")` for tests) and, for a native `asearch`, an `AsyncQdrantClient`.

Points are stored the way LangChain's Qdrant integration stores them: the text under
`page_content` and the metadata under `metadata`, so collections built with LangChain can be searched
with permissions as they are. Filters address `metadata.<field>`; for list fields Qdrant matches when
any element matches, which is the reference semantics of `In`. Field names used in filters may not
contain `.`, `[`, `]` or `"`, which Qdrant reads as path syntax.

Qdrant point ids are unsigned integers or UUIDs. `add` derives a UUID from the SHA-256 of each
document id, whatever the id looks like, and keeps the original id in the payload under
`wardowl_id`; search results report that id, or the point id for points written by something
else. `set_metadata` and `delete` select points by a filter that also requires `wardowl_id` to be
the id asked for, so they never change a point that belongs to another id. A point written by
something else has no `wardowl_id`; it is reached by its own UUID, written exactly as results
report it.

Create the collection with cosine distance, and index the permission fields: `create_collection`
does both. Without a payload index Qdrant still filters correctly, but slowly on large collections.

For keyword search, `create_collection(..., text_search=True)` adds a sparse vector,
`wardowl_bm25`, with Qdrant's IDF modifier. The backend writes each chunk's BM25 term weights
there (`bm25_terms`), and Qdrant applies the inverse document frequency at search time, so
`QdrantBackend(..., mode="keyword")` ranks by BM25 under the same `query_filter`, with no model.
"""

from __future__ import annotations

import asyncio
import re
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from wardowl.backends._errors import required
from wardowl.backends._ids import canonical_uuid, hashed_uuid
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Hit, Query

if TYPE_CHECKING:
    from qdrant_client import models

__all__ = [
    "ID_KEY",
    "SPARSE_NAME",
    "QdrantBackend",
    "bm25_terms",
    "create_collection",
    "filter_to_qdrant",
    "point_id",
]

Embed = Callable[[str], Sequence[float]]

ID_KEY = "wardowl_id"
"""Payload key holding the document id that `add` was given."""

SPARSE_NAME = "wardowl_bm25"
"""The sparse vector holding each chunk's BM25 term weights, for `mode="keyword"`."""

_PATH_SYNTAX = frozenset('.[]"')
_BATCH = 256
_WORDS = re.compile(r"\w+")
_K1, _B, _AVG_LEN = 1.2, 0.75, 256.0  # BM25's usual k1 and b, and Qdrant's own average length


def _tokens(text: str) -> list[str]:
    return _WORDS.findall(text.lower())


def bm25_terms(text: str) -> tuple[list[int], list[float]]:
    """A chunk's BM25 term weights, as the indices and values of the sparse vector `wardowl_bm25`.

    Words are runs of letters, digits and underscores, lowercased; every word is kept. Each word's
    index is its CRC-32, and its value BM25's term-frequency part, `tf * (k1 + 1) / (tf + k1 *
    (1 - b + b * length / 256))` with k1 = 1.2 and b = 0.75. Qdrant multiplies in the inverse
    document frequency at search time. Use it to fill the field for points written by other tools.
    """
    counts = Counter(zlib.crc32(t.encode("utf-8")) for t in _tokens(text))
    norm = _K1 * (1 - _B + _B * sum(counts.values()) / _AVG_LEN)
    indices = sorted(counts)
    return indices, [counts[i] * (_K1 + 1) / (counts[i] + norm) for i in indices]


def _query_terms(text: str) -> tuple[list[int], list[float]]:
    indices = sorted({zlib.crc32(t.encode("utf-8")) for t in _tokens(text)})
    return indices, [1.0] * len(indices)


def point_id(document_id: str) -> str:
    """The Qdrant point id `add` writes a document id to: a UUID derived from its SHA-256.

    Every id is hashed, one that is already a UUID too, so no two document ids share a point.
    """
    return hashed_uuid(document_id, "wardowl qdrant point")


def _addressed(ids: Sequence[str]) -> models.Filter:
    """The points that hold these document ids: the point `add` writes each to, if its `wardowl_id`
    is that id, and the point named by an id in canonical UUID form that has no `wardowl_id`."""
    from qdrant_client import models

    points: list[int | str] = [point_id(i) for i in ids]
    own = models.Filter(
        must=[
            models.HasIdCondition(has_id=points),
            models.FieldCondition(key=ID_KEY, match=models.MatchAny(any=list(ids))),
        ]
    )
    foreign: list[int | str] = [i for i in ids if canonical_uuid(i)]
    if not foreign:
        return own
    written_elsewhere = models.Filter(
        must=[
            models.HasIdCondition(has_id=foreign),
            models.IsEmptyCondition(is_empty=models.PayloadField(key=ID_KEY)),
        ]
    )
    return models.Filter(should=[own, written_elsewhere])


def _key(metadata_key: str, field: str) -> str:
    if not field or _PATH_SYNTAX & set(field):
        raise ValueError(f"field {field!r} cannot be filtered in Qdrant: '.', '[', ']' and '\"' are path syntax")
    return f"{metadata_key}.{field}"


def filter_to_qdrant(f: Filter, metadata_key: str = "metadata") -> models.Filter:
    """Translate a filter into a Qdrant `Filter`. Raises `NothingMatches` when nothing can match.

    `In` becomes `MatchAny` on `metadata.<field>`, one condition per value type (strings and
    integers are never mixed in one list), joined with `should`. An `In` with no values matches
    nothing; that is decided here instead of being left to Qdrant's handling of empty lists, and a
    filter that can match nothing raises rather than return something Qdrant reads as no filter.
    """
    return required(_filter(f, metadata_key))


def _filter(f: Filter, metadata_key: str) -> models.Filter | None:
    """The Qdrant `Filter`, or `None` when nothing can match."""
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
    text_search: bool = False,
) -> None:
    """Create a cosine-distance collection if it does not exist, and keyword indexes on the
    permission fields (`metadata.allowed_users` and `metadata.allowed_groups` by default) and on
    `wardowl_id`, which `set_metadata` and `delete` filter on (a collection in strict mode refuses
    filtered updates on fields without an index).

    `text_search=True` also creates the sparse vector `wardowl_bm25` with Qdrant's IDF
    modifier, for `mode="keyword"`. Qdrant can add it to an existing collection only by recreating it.
    """
    from qdrant_client import models

    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    if not client.collection_exists(name):
        params = models.VectorParams(size=dim, distance=models.Distance.COSINE)
        sparse = {SPARSE_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)} if text_search else None
        client.create_collection(
            name, vectors_config=params if vector_name is None else {vector_name: params}, sparse_vectors_config=sparse
        )
    client.create_payload_index(name, ID_KEY, field_schema=models.PayloadSchemaType.KEYWORD)
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

    `mode="keyword"` ranks by BM25 instead, on the sparse vector `wardowl_bm25` of a collection
    created with `text_search=True`, under the same `query_filter`. Scores are BM25, higher for
    better matches. A vector backend writes that sparse vector too when given `text_search=True`,
    so one collection serves both halves of hybrid search. A keyword backend without `embed`
    writes chunks with only the sparse vector, which keyword search finds and vector search does not.
    """

    supports_filters = True

    def __init__(
        self,
        client: Any,
        collection: str,
        embed: Embed | None = None,
        *,
        mode: str = "vector",
        text_search: bool = False,
        content_key: str = "page_content",
        metadata_key: str = "metadata",
        vector_name: str | None = None,
        search_params: Any = None,
        async_client: Any = None,
    ) -> None:
        if not collection:
            raise ValueError("collection must be a non-empty name")
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        self.mode = mode
        self.text_search = text_search or mode == "keyword"
        self.client = client
        self.async_client = async_client
        self.collection = collection
        self.embed = embed
        self.content_key = content_key
        self.metadata_key = metadata_key
        self.vector_name = vector_name
        self.search_params = search_params

    # ---- writing -----------------------------------------------------------------------------

    def _vector(self, text: str, dense: Sequence[float] | None) -> Any:
        from qdrant_client import models

        values = None if dense is None else [float(x) for x in dense]
        if not self.text_search:
            return values if self.vector_name is None else {self.vector_name: values}
        vectors: dict[str, Any] = {}
        if values is not None:
            vectors[self.vector_name or ""] = values  # "" is Qdrant's name for the unnamed vector
        indices, weights = bm25_terms(text)
        vectors[SPARSE_NAME] = models.SparseVector(indices=indices, values=weights)
        return vectors

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        from qdrant_client import models

        docs = list(documents)
        vectors: Sequence[Sequence[float] | None]
        if embeddings is None and self.embed is None and self.mode == "keyword":
            vectors = [None] * len(docs)  # a keyword-only backend without embed writes no dense vector
        else:
            vectors = document_vectors(docs, embeddings, self.embed)
        points = [
            models.PointStruct(
                id=point_id(d.id),
                vector=self._vector(d.text, v),
                payload={ID_KEY: d.id, self.content_key: d.text, self.metadata_key: dict(d.metadata)},
            )
            for d, v in zip(docs, vectors, strict=True)
        ]
        for start in range(0, len(points), _BATCH):
            self.client.upsert(self.collection, points=points[start : start + _BATCH], wait=True)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        Each change sets only the metadata key of the payload (Qdrant replaces a top-level key's
        whole value, so no key of the old metadata survives), on the points that hold that id. It
        never writes the text back, and a point deleted meanwhile is not recreated.
        """
        from qdrant_client import models

        operations: list[Any] = [
            models.SetPayloadOperation(
                set_payload=models.SetPayload(payload={self.metadata_key: dict(new)}, filter=_addressed([doc_id]))
            )
            for doc_id, new in metadata.items()
        ]
        for start in range(0, len(operations), _BATCH):
            self.client.batch_update_points(
                self.collection, update_operations=operations[start : start + _BATCH], wait=True
            )

    def delete(self, ids: Iterable[str]) -> None:
        """Remove chunks by id: the points holding each id, and no other. Ids not stored are ignored."""
        from qdrant_client import models

        selected = list(dict.fromkeys(ids))
        for start in range(0, len(selected), _BATCH):
            chosen = models.FilterSelector(filter=_addressed(selected[start : start + _BATCH]))
            self.client.delete(self.collection, points_selector=chosen, wait=True)

    # ---- searching ---------------------------------------------------------------------------

    def _request(self, query: Query, k: int, f: Filter | None) -> dict[str, Any] | None:
        query_filter = None
        if f is not None:
            query_filter = _filter(f, self.metadata_key)
            if query_filter is None:
                return None  # nothing can match
        search: Any
        using: str | None
        if self.mode == "keyword":
            from qdrant_client import models

            indices, weights = _query_terms(query.keywords())
            if not indices:
                return None  # no word to match
            search, using = models.SparseVector(indices=indices, values=weights), SPARSE_NAME
        else:
            search, using = query.embedded(self.embed), self.vector_name
        return {
            "collection_name": self.collection,
            "query": search,
            "using": using,
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
