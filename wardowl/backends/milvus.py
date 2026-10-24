"""Milvus backend: the permission filter is applied inside Milvus's vector search.

Install with `pip install "wardowl[milvus]"`. Pass a `pymilvus.MilvusClient` and, for a native
`asearch`, a `pymilvus.AsyncMilvusClient`.

Permissions are matched on one array field, `wardowl_terms`, holding a type-tagged term for
every string or integer in the chunk's metadata, as the Elasticsearch backend does. A filter
becomes `ARRAY_CONTAINS_ANY` conditions on that field, and the values travel as filter parameters
(`filter_params`), never inside the expression text, so no group name or user id can change the
expression. Tagged terms keep `1` and `"1"` apart and never match booleans, floats, nested lists or
objects, as the reference semantics require. The metadata itself is stored in a JSON field and
returned as written, for the retriever's re-check.

`create_collection` sets up the schema: a string primary key, the vector with an HNSW index, the
text, the metadata, and the terms with an inverted index. Searches use strong consistency by
default, so a chunk is found as soon as `add` returns. Scores are Milvus's similarity, so use the
`COSINE` (default) or `IP` metric, where higher means closer.

With `create_collection(..., text_search=True)` Milvus also derives a BM25 sparse vector from the
text, and `MilvusBackend(..., mode="keyword")` searches it under the same permission filter.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from wardowl.backends._engines import _SEP, TERMS_FIELD, tag, terms
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Hit, Query

__all__ = [
    "SPARSE_FIELD",
    "TERMS_FIELD",
    "MilvusBackend",
    "create_collection",
    "filter_to_expression",
    "permission_terms",
]

Embed = Callable[[str], Sequence[float]]

ID_FIELD = "id"
SPARSE_FIELD = "wardowl_sparse"
"""The BM25 sparse vector Milvus derives from the text, for `mode="keyword"`."""

_BATCH = 256
_WORD = re.compile(r"\w")


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `wardowl_terms` for a chunk with this metadata, for rows written by other tools."""
    return terms("metadata", metadata)


def _prune(f: Filter) -> Filter | None:
    """The same filter without the parts that can match nothing, or None when nothing can match."""
    if isinstance(f, In):
        return f if f.values else None
    if isinstance(f, And):
        clauses = [_prune(c) for c in f.clauses]
        if any(c is None for c in clauses):
            return None
        return And(*(c for c in clauses if c is not None))
    if isinstance(f, Or):
        kept = [c for c in (_prune(c) for c in f.clauses) if c is not None]
        return Or(*kept) if kept else None
    raise TypeError(f"not a filter: {f!r}")


def filter_to_expression(f: Filter) -> tuple[str, dict[str, list[str]]] | None:
    """Translate a filter into a Milvus filter template and its parameters.

    Returns `(expression, params)` for `search(filter=expression, filter_params=params)`, or
    `None` when nothing can match. Each `In` becomes `ARRAY_CONTAINS_ANY(wardowl_terms, {pN})`
    with the tagged terms in `params["pN"]`.
    """
    pruned = _prune(f)
    if pruned is None:
        return None
    params: dict[str, list[str]] = {}

    def emit(node: Filter) -> str:
        if isinstance(node, In):
            if _SEP in node.field:
                raise ValueError(f"field {node.field!r} may not contain \\x1f")
            name = f"p{len(params)}"
            params[name] = [tag(node.field, v) for v in _ordered(node.values)]
            return f"ARRAY_CONTAINS_ANY({TERMS_FIELD}, {{{name}}})"
        parts = [emit(c) for c in node.clauses]
        if len(parts) == 1:
            return parts[0]
        return "(" + (" AND " if isinstance(node, And) else " OR ").join(parts) + ")"

    return emit(pruned), params


def create_collection(
    client: Any,
    name: str,
    dim: int,
    *,
    metric: str = "COSINE",
    vector_field: str = "embedding",
    content_field: str = "content",
    metadata_field: str = "metadata",
    max_id_length: int = 512,
    max_content_length: int = 65_535,
    max_terms: int = 4096,
    max_term_length: int = 1024,
    hnsw: dict[str, int] | None = None,
    text_search: bool = False,
) -> None:
    """Create a collection `MilvusBackend` can use, with an HNSW index on the vector and an inverted
    index on the permission terms.

    `max_terms` caps the tagged terms per chunk: one per string or integer in its metadata, so a
    chunk shared with many groups needs more. `max_term_length` caps one term, which is the field
    name, two characters and the value.

    `text_search=True` analyzes the text with Milvus's standard analyzer, which keeps every word,
    and adds a BM25 function that fills a sparse vector, `wardowl_sparse`, with an inverted index,
    for `mode="keyword"`. Milvus can only add it when the collection is created.
    """
    from pymilvus import DataType, MilvusClient

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(ID_FIELD, DataType.VARCHAR, is_primary=True, max_length=max_id_length)
    schema.add_field(vector_field, DataType.FLOAT_VECTOR, dim=dim)
    analyzed = {"enable_analyzer": True} if text_search else {}
    schema.add_field(content_field, DataType.VARCHAR, max_length=max_content_length, **analyzed)
    schema.add_field(metadata_field, DataType.JSON)
    schema.add_field(
        TERMS_FIELD, DataType.ARRAY, element_type=DataType.VARCHAR, max_capacity=max_terms, max_length=max_term_length
    )
    indexes = client.prepare_index_params()
    indexes.add_index(
        field_name=vector_field,
        index_type="HNSW",
        metric_type=metric,
        params=hnsw or {"M": 16, "efConstruction": 200},
    )
    indexes.add_index(field_name=TERMS_FIELD, index_type="INVERTED")
    if text_search:
        from pymilvus import Function, FunctionType

        schema.add_field(SPARSE_FIELD, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="wardowl_bm25",
                function_type=FunctionType.BM25,
                input_field_names=[content_field],
                output_field_names=[SPARSE_FIELD],
            )
        )
        indexes.add_index(field_name=SPARSE_FIELD, index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    client.create_collection(collection_name=name, schema=schema, index_params=indexes, consistency_level="Strong")


class MilvusBackend:
    """Vector search on Milvus, with the permission filter applied inside the search.

    `embed` turns text into a vector (your embedding model). `ef` is HNSW's search breadth: higher
    finds more of the true nearest neighbours, more slowly; it is raised to `k` when `k` is larger,
    as Milvus requires. `search_params` replaces the search parameters entirely, for another index
    type. `consistency_level` is passed to every search ("Strong" by default, so writes are visible
    at once; "Bounded" is faster when a short delay is acceptable). Pass `async_client` (an
    `AsyncMilvusClient`) for a native `asearch`; otherwise async calls run `search` in a thread.
    Without `embed`, search with `Query(vector=...)` and pass `embeddings` to `add`.

    `mode="keyword"` searches the BM25 sparse vector of a collection created with
    `text_search=True` instead, under the same permission filter, with the query's text. Scores are
    Milvus's BM25, higher for better matches; `search_params` is passed only when given. Milvus
    requires every chunk's vector, so `add` still needs `embed` or `embeddings` in this mode.
    """

    supports_filters = True

    def __init__(
        self,
        client: Any,
        collection: str,
        embed: Embed | None = None,
        *,
        mode: str = "vector",
        vector_field: str = "embedding",
        content_field: str = "content",
        metadata_field: str = "metadata",
        ef: int = 100,
        search_params: dict[str, Any] | None = None,
        consistency_level: str = "Strong",
        async_client: Any = None,
    ) -> None:
        if not collection:
            raise ValueError("collection must be a non-empty name")
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        if ef < 1:
            raise ValueError("ef must be positive")
        self.mode = mode
        self.client = client
        self.async_client = async_client
        self.collection = collection
        self.embed = embed
        self.vector_field = vector_field
        self.content_field = content_field
        self.metadata_field = metadata_field
        self.ef = ef
        self.search_params = search_params
        self.consistency_level = consistency_level

    # ---- writing -----------------------------------------------------------------------------

    def _row(self, doc: Document, vector: Sequence[float]) -> dict[str, Any]:
        return {
            ID_FIELD: doc.id,
            self.vector_field: [float(x) for x in vector],
            self.content_field: doc.text,
            self.metadata_field: dict(doc.metadata),
            TERMS_FIELD: terms(doc.id, doc.metadata),
        }

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        docs = list(documents)
        vectors = document_vectors(docs, embeddings, self.embed)
        rows = [self._row(d, v) for d, v in zip(docs, vectors, strict=True)]
        for start in range(0, len(rows), _BATCH):
            self.client.upsert(collection_name=self.collection, data=rows[start : start + _BATCH])

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored."""
        ids = list(metadata)
        for start in range(0, len(ids), _BATCH):
            stored = self.client.get(
                collection_name=self.collection,
                ids=ids[start : start + _BATCH],
                output_fields=[self.vector_field, self.content_field],
                consistency_level="Strong",  # rows written just before must be found
            )
            # Whole rows, read and written back: the result does not depend on how a partial update merges.
            rows = [
                self._row(Document(r[ID_FIELD], r[self.content_field], metadata[r[ID_FIELD]]), r[self.vector_field])
                for r in stored
            ]
            if rows:
                self.client.upsert(collection_name=self.collection, data=rows)

    def delete(self, ids: Iterable[str]) -> None:
        selected = list(ids)
        if selected:
            self.client.delete(collection_name=self.collection, ids=selected)

    # ---- searching ---------------------------------------------------------------------------

    def _request(self, query: Query, k: int, f: Filter | None) -> dict[str, Any] | None:
        request: dict[str, Any] = {
            "collection_name": self.collection,
            "limit": k,
            "output_fields": [self.content_field, self.metadata_field],
            "consistency_level": self.consistency_level,
        }
        if self.mode == "keyword":
            text = query.keywords()
            if not _WORD.search(text):
                return None  # no word to match
            request.update(data=[text], anns_field=SPARSE_FIELD)
            if self.search_params is not None:
                request["search_params"] = self.search_params
        else:
            request.update(data=[query.embedded(self.embed)], anns_field=self.vector_field)
            request["search_params"] = self.search_params or {"params": {"ef": max(self.ef, k)}}
        if f is not None:
            compiled = filter_to_expression(f)
            if compiled is None:
                return None  # nothing can match
            request["filter"], request["filter_params"] = compiled
        return request

    def _hits(self, results: Any) -> list[Hit]:
        hits = []
        for found in results[0] if results else []:
            entity = found.get("entity") or {}
            text = entity.get(self.content_field)
            metadata = entity.get(self.metadata_field)
            hits.append(
                Hit(
                    str(found.get("id")),
                    text if isinstance(text, str) else "",
                    float(found.get("distance")),
                    # A row without a metadata object carries no permissions: the re-check denies it.
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
        return self._hits(self.client.search(**request))

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_client is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        request = await asyncio.to_thread(self._request, Query.of(query), k, filter)
        if request is None:
            return []
        return self._hits(await self.async_client.search(**request))
