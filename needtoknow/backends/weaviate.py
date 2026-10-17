"""Weaviate backend: the permission filter is applied inside Weaviate's vector search.

Install with `pip install "needtoknow[weaviate]"`. Pass a connected `weaviate.WeaviateClient` (for
example `weaviate.connect_to_local()` or `weaviate.connect_to_weaviate_cloud(...)`) and, for a
native `asearch`, a connected `weaviate.WeaviateAsyncClient`.

Permissions are matched on one `text[]` property, `needtoknow_terms`, holding a type-tagged term for
every string or integer in the chunk's metadata, as the Elasticsearch, Milvus and MongoDB backends
do. The property uses `field` tokenization, so each term is one token, compared exactly and with
its case. A filter becomes `contains_any` conditions joined with `all_of` and `any_of`, built with
the client's filter objects, so values are data, never query syntax. The metadata is stored as JSON
text, unindexed, and returned exactly as written, for the retriever's re-check.

Weaviate object ids are UUIDs: `add` derives one from each document id (`point_id`) and keeps the
original id in the `needtoknow_id` property. `create_collection` sets up the collection with a
self-provided cosine vector.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from needtoknow.backends._engines import _SEP, TERMS_FIELD, tag, terms
from needtoknow.backends._vectors import document_vectors
from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Hit, Query

__all__ = [
    "ID_PROPERTY",
    "TERMS_FIELD",
    "WeaviateBackend",
    "create_collection",
    "filter_to_weaviate",
    "permission_terms",
    "point_id",
]

Embed = Callable[[str], Sequence[float]]

ID_PROPERTY = "needtoknow_id"
"""Property holding the document id that `add` was given."""

_NAMESPACE = uuid.UUID("0b6f3c52-8d1e-4a7b-9c35-2f4e8a1d6b90")
_BATCH = 200


def _guarded(term: str) -> str:
    # `field` tokenization may trim surrounding whitespace; a separator at both ends keeps a value
    # such as "hr " distinct from "hr".
    return f"{_SEP}{term}{_SEP}"


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `needtoknow_terms` for a chunk with this metadata, for objects written by other tools."""
    return [_guarded(t) for t in terms("metadata", metadata)]


def point_id(document_id: str) -> str:
    """The Weaviate object id for a document id: the id itself if it is a UUID, otherwise one derived from it."""
    try:
        return str(uuid.UUID(document_id))
    except ValueError:
        return str(uuid.uuid5(_NAMESPACE, document_id))


def filter_to_weaviate(f: Filter) -> Any:
    """Translate a filter into a Weaviate filter on the tagged terms, or `None` when nothing can match."""
    from weaviate.classes.query import Filter as Where

    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        if not values:
            return None
        return Where.by_property(TERMS_FIELD).contains_any([_guarded(tag(f.field, v)) for v in values])
    if isinstance(f, And):
        parts = [filter_to_weaviate(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else Where.all_of(parts)
    if isinstance(f, Or):
        kept = [p for p in (filter_to_weaviate(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else Where.any_of(kept)
    raise TypeError(f"not a filter: {f!r}")


def create_collection(
    client: Any,
    name: str,
    *,
    vector_name: str = "embedding",
    content_property: str = "content",
    metadata_property: str = "metadata_json",
) -> None:
    """Create a collection `WeaviateBackend` can use: a self-provided cosine HNSW vector, the text,
    the metadata as unindexed JSON, and the id and permission terms with `field` tokenization."""
    from weaviate.classes.config import Configure, DataType, Property, Tokenization, VectorDistances

    client.collections.create(
        name,
        vector_config=[
            Configure.Vectors.self_provided(
                name=vector_name,
                vector_index_config=Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE),
            )
        ],
        properties=[
            Property(
                name=ID_PROPERTY,
                data_type=DataType.TEXT,
                tokenization=Tokenization.FIELD,
                index_searchable=False,
            ),
            Property(name=content_property, data_type=DataType.TEXT),
            Property(
                name=metadata_property,
                data_type=DataType.TEXT,
                index_filterable=False,
                index_searchable=False,
            ),
            Property(
                name=TERMS_FIELD,
                data_type=DataType.TEXT_ARRAY,
                tokenization=Tokenization.FIELD,
                index_searchable=False,
            ),
        ],
    )


class WeaviateBackend:
    """Vector search on Weaviate, with the permission filter applied inside the search.

    `embed` turns text into a vector (your embedding model). `vector_name` is the collection's named
    vector. Scores are cosine similarity, 1 minus Weaviate's cosine distance. Pass `async_client` (a
    connected `WeaviateAsyncClient`) for a native `asearch`; otherwise async calls run `search` in a
    thread. Without `embed`, search with `Query(vector=...)` and pass `embeddings` to `add`.
    """

    supports_filters = True

    def __init__(
        self,
        client: Any,
        collection: str,
        embed: Embed | None = None,
        *,
        vector_name: str = "embedding",
        content_property: str = "content",
        metadata_property: str = "metadata_json",
        async_client: Any = None,
    ) -> None:
        if not collection:
            raise ValueError("collection must be a non-empty name")
        self.client = client
        self.async_client = async_client
        self.collection = collection
        self.embed = embed
        self.vector_name = vector_name
        self.content_property = content_property
        self.metadata_property = metadata_property

    # ---- writing -----------------------------------------------------------------------------

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        from weaviate.classes.data import DataObject

        docs = list(documents)
        vectors = document_vectors(docs, embeddings, self.embed)
        objects = [
            DataObject(
                uuid=point_id(d.id),
                properties={
                    ID_PROPERTY: d.id,
                    self.content_property: d.text,
                    self.metadata_property: json.dumps(dict(d.metadata), sort_keys=True),
                    TERMS_FIELD: [_guarded(t) for t in terms(d.id, d.metadata)],
                },
                vector={self.vector_name: [float(x) for x in v]},
            )
            for d, v in zip(docs, vectors, strict=True)
        ]
        handle = self.client.collections.get(self.collection)
        for start in range(0, len(objects), _BATCH):
            result = handle.data.insert_many(objects[start : start + _BATCH])
            if result.errors:
                first = next(iter(result.errors.values()))
                raise RuntimeError(f"Weaviate rejected {len(result.errors)} objects, for example: {first}")

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored."""
        handle = self.client.collections.get(self.collection)
        for doc_id, new in metadata.items():
            uid = point_id(doc_id)
            if handle.data.exists(uid):  # an update of a missing object would fail
                # An update replaces the value of each property it names, and leaves the rest.
                properties = {
                    self.metadata_property: json.dumps(dict(new), sort_keys=True),
                    TERMS_FIELD: [_guarded(t) for t in terms(doc_id, new)],
                }
                handle.data.update(uuid=uid, properties=properties)

    def delete(self, ids: Iterable[str]) -> None:
        from weaviate.classes.query import Filter as Where

        selected = [point_id(i) for i in dict.fromkeys(ids)]
        if selected:
            handle = self.client.collections.get(self.collection)
            handle.data.delete_many(where=Where.by_id().contains_any(selected))

    # ---- searching ---------------------------------------------------------------------------

    def _request(self, query: Query, k: int, f: Filter | None) -> dict[str, Any] | None:
        from weaviate.classes.query import MetadataQuery

        where = None
        if f is not None:
            where = filter_to_weaviate(f)
            if where is None:
                return None  # nothing can match
        return {
            "near_vector": query.embedded(self.embed),
            "target_vector": self.vector_name,
            "limit": k,
            "filters": where,
            "return_metadata": MetadataQuery(distance=True),
            "return_properties": [ID_PROPERTY, self.content_property, self.metadata_property],
        }

    def _hit(self, obj: Any) -> Hit:
        properties = obj.properties or {}
        doc_id = properties.get(ID_PROPERTY)
        text = properties.get(self.content_property)
        try:
            metadata = json.loads(properties.get(self.metadata_property) or "")
        except (TypeError, ValueError):
            metadata = None
        distance = obj.metadata.distance if obj.metadata is not None else None
        return Hit(
            doc_id if isinstance(doc_id, str) else str(obj.uuid),
            text if isinstance(text, str) else "",
            1.0 - float(distance) if distance is not None else 0.0,
            # An object without readable metadata carries no permissions: the re-check denies it.
            metadata if isinstance(metadata, dict) else {},
        )

    def _hits(self, response: Any) -> list[Hit]:
        hits = [self._hit(o) for o in response.objects]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        request = self._request(Query.of(query), k, filter)
        if request is None:
            return []
        return self._hits(self.client.collections.get(self.collection).query.near_vector(**request))

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_client is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        request = await asyncio.to_thread(self._request, Query.of(query), k, filter)
        if request is None:
            return []
        handle = self.async_client.collections.get(self.collection)
        return self._hits(await handle.query.near_vector(**request))
