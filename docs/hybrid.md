# Hybrid search

Keyword search finds exact terms, product codes and names that embeddings blur; vector search
finds meaning that shares no words with the query. Most production RAG combines both.
`RankFusion` does that while keeping permission-aware retrieval exact: it is a backend that
searches several backends with the same permission filter and fuses their rankings.

```python
from needtoknow import PermissionedRetriever, RankFusion
from needtoknow.backends.elasticsearch import ElasticsearchBackend

vector = ElasticsearchBackend(client, "docs", embed)  # kNN search
keyword = ElasticsearchBackend(client, "docs", None, mode="keyword")  # BM25 on the same index
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

Any backends combine: two indexes, two stores, or a store and a LangChain retriever. `mode="keyword"`
needs no embedding function and runs under the same permission filter as the vector search:

- **Elasticsearch and OpenSearch:** a BM25 `match` query on the text.
- **Weaviate:** Weaviate's own BM25 on the text property, with the same permission filter as its
  vector search, so one collection serves both.
- **Redis:** the Query Engine's BM25 on the text, in an index created with
  `create_index(..., content_field="content")`, so one index serves both
  ([Redis](integrations/redis.md#keyword-search-and-hybrid-search)).
- **LanceDB:** LanceDB's full-text index (BM25) on the text, built by
  `create_indexes(table, content_field="content")`, so one table serves both
  ([LanceDB](integrations/lancedb.md#keyword-search-and-hybrid-search)).
- **PostgreSQL + pgvector:** PostgreSQL full-text search, so one table serves both searches:

```python
from needtoknow.backends.pgvector import PgVectorBackend, create_schema

create_schema(pool, dim=768, text_search="english")  # adds the full-text index
vector = PgVectorBackend(pool, embed)
keyword = PgVectorBackend(pool, None, mode="keyword", text_search="english")
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

## How results are fused

Reciprocal rank fusion scores a chunk by its rank in each backend's results, not by the backends'
own scores, which are not comparable (cosine similarity and BM25 have different scales):

```
score(chunk) = sum over backends that returned it of  weight / (rank_constant + rank)
```

- `weights` (one per backend, default 1) favour one kind of search.
- `rank_constant` (default 60) damps the advantage of the very top ranks.
- `depth` is how many results each backend is asked for (default: the `k` requested). Asking for
  more lets a chunk ranked lower in one search be lifted by the other.

`aretrieve` searches the backends concurrently.

## Why fused results stay permitted

- Every backend receives the filter the retriever passes, so with pre-filtering each fused chunk
  was selected by a backend that applied the permission filter, and the retriever re-checks the
  results as it does for any backend.
- Filters are used only when every backend supports them; otherwise the retriever post-filters the
  fused results, checking each candidate.
- A chunk that two backends return with **different metadata** is left out and logged on the
  `needtoknow` logger. That happens when one index has not caught up with a permission change: the
  chunk's permissions are uncertain, so it is not returned with whichever version came first.
  Monitor the warning; it means your indexes have drifted apart.
