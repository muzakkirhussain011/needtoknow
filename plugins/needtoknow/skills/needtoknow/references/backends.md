# Backends: where the vectors live

Every backend has `search(query, k, filter=None) -> list[Hit]`; most also have `add(documents,
embeddings=None)`, `delete(ids)` and a native `asearch`. `embed` is your embedding function,
`str -> list[float]`. Backends that pre-filter apply the permission filter inside the vector search.

| Store | Import | Set up | Pre-filters |
|---|---|---|---|
| PostgreSQL + pgvector | `from needtoknow.backends.pgvector import PgVectorBackend, create_schema` | `create_schema(pool, dim)` then `PgVectorBackend(pool, embed)` | yes, in SQL |
| Qdrant | `from needtoknow.backends.qdrant import QdrantBackend, create_collection` | `create_collection(client, "docs", dim)` then `QdrantBackend(client, "docs", embed)` | yes |
| Chroma | `from needtoknow.backends.chroma import ChromaBackend` | `ChromaBackend(collection, embed=None)` | yes |
| Elasticsearch | `from needtoknow.backends.elasticsearch import ElasticsearchBackend, create_index` | `create_index(client, "docs", dim)` then `ElasticsearchBackend(client, "docs", embed)` | yes, inside kNN |
| OpenSearch | `from needtoknow.backends.opensearch import OpenSearchBackend, create_index` | `create_index(client, "docs", dim)` then `OpenSearchBackend(client, "docs", embed)` | yes, inside k-NN |
| In memory (tests, examples) | `from needtoknow import InMemoryBackend` | `InMemoryBackend(documents)` | yes |
| Any LangChain vector store | `from needtoknow.langchain import LangChainVectorStoreBackend` | `LangChainVectorStoreBackend(vectorstore)` | no (post-filtered) |
| Any LlamaIndex index | `from needtoknow.llamaindex import LlamaIndexBackend` | `LlamaIndexBackend(index)` | no (post-filtered) |

Install the extra for the store: `needtoknow[pgvector]`, `[qdrant]`, `[chroma]`, `[elasticsearch]`,
`[opensearch]`, `[langchain]`, `[llamaindex]`.

## Useful options

- `PgVectorBackend(pool, embed, table="needtoknow_chunks", ef_search=None, async_pool=None)`:
  `ef_search=100` searches the HNSW index deeper (higher recall, slower); pass an
  `AsyncConnectionPool` as `async_pool` for native async.
- `QdrantBackend(client, collection, embed, search_params=None, vector_name=None, async_client=None)`:
  `search_params=models.SearchParams(hnsw_ef=128)`; points use LangChain's layout (`page_content`,
  `metadata`), so LangChain-built collections work as they are.
- `ElasticsearchBackend(..., num_candidates=100)`; `create_index(..., index_options={"type": "hnsw"})`
  keeps full-precision vectors (Elasticsearch quantizes by default).
- Elasticsearch and OpenSearch match permissions on a tagged keyword field, `needtoknow_terms`.
  Documents written by other tools need it: `doc["needtoknow_terms"] = permission_terms(doc["metadata"])`.

## Documents

```python
from needtoknow import Document

Document(id="handbook#3", text="Annual leave is 25 days.", metadata={"allowed_groups": ["*"], "doc_id": "handbook"})
```

Metadata values used in filters are strings or integers (or lists of them); booleans and floats
never match a filter. Give every chunk of a source document the same permissions, and `doc_id` when
an authorization service decides per document.

## A store not listed

Implement `search(query, k, filter=None)` returning `Hit(id, text, score, metadata)` best first,
set `supports_filters = True` only if `search` applies `filter` exactly, and check it:

```python
from needtoknow.testing import check_backend

check_backend(lambda documents: make_my_backend(documents))
```
