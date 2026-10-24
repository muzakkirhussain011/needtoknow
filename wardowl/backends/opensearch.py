"""OpenSearch backend: k-NN search with the permission filter applied during the search.

Install the client with `pip install "wardowl[opensearch]"` and pass an `OpenSearch` client,
and an `AsyncOpenSearch` for a native `asearch`. Needs OpenSearch 2.9 or newer.

The filter goes in the `knn` query's `filter`, which the Lucene and Faiss engines apply during the
search (efficient k-NN filtering), so the k nearest chunks the subject may read come back however
few of the index's documents that is.

As in the Elasticsearch backend, permissions are matched on one type-tagged keyword field,
`wardowl_terms`, because keyword fields compare strings; the metadata is stored unindexed and
returned exactly as written. `create_index` sets this up. To make an index written by another tool
searchable with permissions, add `wardowl_terms` to its documents with `permission_terms(metadata)`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wardowl.backends._engines import TERMS_FIELD, EngineBackend, filter_to_query, mappings, terms

__all__ = ["TERMS_FIELD", "OpenSearchBackend", "create_index", "filter_to_query", "permission_terms"]


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `wardowl_terms` for a chunk with this metadata."""
    return terms("metadata", metadata)


def create_index(
    client: Any,
    index: str,
    dim: int,
    *,
    engine: str = "lucene",
    parameters: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
    vector_field: str = "embedding",
    content_field: str = "content",
    metadata_field: str = "metadata",
) -> None:
    """Create the index if it does not exist, with a cosine `knn_vector` field (HNSW).

    `engine` is `"lucene"` (the default) or `"faiss"`; both apply filters during the search.
    `parameters` are the HNSW method parameters, for example `{"m": 16, "ef_construction": 128}`.
    """
    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    if client.indices.exists(index=index):
        return
    method: dict[str, Any] = {"name": "hnsw", "engine": engine, "space_type": "cosinesimil"}
    if parameters is not None:
        method["parameters"] = dict(parameters)
    vector = {"type": "knn_vector", "dimension": dim, "method": method}
    # Your settings, with k-NN switched on whatever they say about the index.
    index_settings: dict[str, Any] = dict(settings or {})
    index_settings["index"] = {**dict(index_settings.get("index") or {}), "knn": True}
    body = {"settings": index_settings, "mappings": mappings(vector, vector_field, content_field, metadata_field)}
    client.indices.create(index=index, body=body)


class OpenSearchBackend(EngineBackend):
    """Search an OpenSearch index with the permission filter applied during the k-NN search.

    `embed` turns text into a vector; without it, search with `Query(vector=...)` and pass
    `embeddings` to `add`. Scores are OpenSearch's: `(1 + cosine) / 2` for a `cosinesimil` field.
    `refresh` is passed to bulk writes (`"wait_for"` by default, so written chunks are searchable
    when `add` returns). `num_candidates` is not used by OpenSearch's `knn` query; set `ef_search`
    in the index settings instead. `mode="keyword"` searches the text with
    BM25 instead, under the same permission filter, for hybrid search with `RankFusion`; `embed`
    may then be None.
    """

    _engine = "OpenSearch"

    def _search_request(self, vector: list[float], k: int, query: dict[str, Any] | None) -> dict[str, Any]:
        clause: dict[str, Any] = {"vector": vector, "k": k}
        if query is not None:
            clause["filter"] = query
        return {
            "body": {
                "size": k,
                "query": {"knn": {self.vector_field: clause}},
                "_source": {"excludes": [self.vector_field]},
            }
        }

    def _keyword_request(self, text: str, k: int, query: dict[str, Any] | None) -> dict[str, Any]:
        match = {"bool": {"must": [{"match": {self.content_field: text}}], "filter": [query] if query else []}}
        return {"body": {"size": k, "query": match, "_source": {"excludes": [self.vector_field]}}}

    def _bulk(self, client: Any, operations: list[dict[str, Any]]) -> Any:
        return client.bulk(body=operations, refresh=self.refresh)
