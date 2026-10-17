"""Elasticsearch backend: approximate kNN search with the permission filter inside the kNN clause.

Install the client with `pip install "needtoknow[elasticsearch]"` and pass an `Elasticsearch`
client, and an `AsyncElasticsearch` for a native `asearch`. Needs Elasticsearch 8.12 or newer.

The filter goes in the `knn` clause's `filter`, which Elasticsearch applies during the approximate
search, so the k nearest chunks the subject may read come back however few of the index's
documents that is. (A filter beside the `knn` clause would apply after it and could return fewer.)

Permissions are matched on one type-tagged keyword field, `needtoknow_terms`, rather than on the
metadata fields: keyword fields compare strings, so a `terms` query for the integer `1` would also
match `"1"`. The metadata is stored unindexed (`enabled: false`) and returned exactly as written,
and arbitrary metadata never adds mappings. `create_index` sets this up. To make an index written
by another tool searchable with permissions, add `needtoknow_terms` to its documents with
`permission_terms(metadata)`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from needtoknow.backends._engines import _MAX_K, TERMS_FIELD, EngineBackend, filter_to_query, mappings, terms

__all__ = ["TERMS_FIELD", "ElasticsearchBackend", "create_index", "filter_to_query", "permission_terms"]


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `needtoknow_terms` for a chunk with this metadata."""
    return terms("metadata", metadata)


def create_index(
    client: Any,
    index: str,
    dim: int,
    *,
    index_options: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
    vector_field: str = "embedding",
    content_field: str = "content",
    metadata_field: str = "metadata",
) -> None:
    """Create the index if it does not exist, with a cosine `dense_vector` field.

    Elasticsearch quantizes dense vectors by default (`int8_hnsw` below 384 dimensions, `bbq_hnsw`
    from 384 in 9.1 and later), which saves memory and costs some recall. Pass
    `index_options={"type": "hnsw"}` to keep full-precision vectors.
    """
    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    if client.indices.exists(index=index):
        return
    vector: dict[str, Any] = {"type": "dense_vector", "dims": dim, "index": True, "similarity": "cosine"}
    if index_options is not None:
        vector["index_options"] = dict(index_options)
    kwargs: dict[str, Any] = {"mappings": mappings(vector, vector_field, content_field, metadata_field)}
    if settings is not None:
        kwargs["settings"] = dict(settings)
    client.indices.create(index=index, **kwargs)


class ElasticsearchBackend(EngineBackend):
    """Search an Elasticsearch index with the permission filter applied during the kNN search.

    `embed` turns text into a vector; without it, search with `Query(vector=...)` and pass
    `embeddings` to `add`. `num_candidates` is how many nearest neighbours each shard considers (at
    least k, at most 10,000): higher finds more of the true nearest chunks and takes longer. Scores
    are Elasticsearch's: `(1 + cosine) / 2` for a cosine field. `refresh` is passed to bulk writes
    (`"wait_for"` by default, so written chunks are searchable when `add` returns).
    `mode="keyword"` searches the text with BM25 instead, under the same permission filter, for
    hybrid search with `RankFusion`; `embed` may then be None.
    """

    def _search_request(self, vector: list[float], k: int, query: dict[str, Any] | None) -> dict[str, Any]:
        knn: dict[str, Any] = {
            "field": self.vector_field,
            "query_vector": vector,
            "k": k,
            "num_candidates": min(max(self.num_candidates, k), _MAX_K),
        }
        if query is not None:
            knn["filter"] = query
        return {"knn": knn, "size": k, "source_excludes": [self.vector_field]}

    def _keyword_request(self, text: str, k: int, query: dict[str, Any] | None) -> dict[str, Any]:
        match = {"bool": {"must": [{"match": {self.content_field: text}}], "filter": [query] if query else []}}
        return {"query": match, "size": k, "source_excludes": [self.vector_field]}

    def _bulk(self, client: Any, operations: list[dict[str, Any]]) -> Any:
        return client.bulk(operations=operations, refresh=self.refresh)
