# Redis vector search

```bash
pip install "needtoknow[redis]"
```

The backend runs a KNN query on the Redis Query Engine with the permission filter as part of the
same query, so the k nearest chunks the subject may read come back however few of the index's
chunks that is. It needs Redis 8 or later, where the Query Engine is built in, or Redis Stack. CI
runs needtoknow's [conformance kit](../extending.md#test-it-with-the-conformance-kit) against
Redis 8.10.2. The same `needtoknow[redis]` extra serves the [shared permission cache](../operations.md#sharing-the-cache-across-processes).

```python
import redis

from needtoknow.backends.redis import RedisBackend, create_index

client = redis.Redis.from_url(REDIS_URL)
create_index(client, "chunks", dim=768)  # hashes under "needtoknow:", HNSW, cosine
backend = RedisBackend(client, "chunks", embed=embed)  # embed: str -> list[float]
backend.add(documents)
```

Each chunk is a hash under the prefix (`needtoknow:` plus the document id) with the fields
`needtoknow_id`, `content`, `metadata` (JSON), `needtoknow_terms` and `embedding` (float32). A write
is searchable as soon as it returns; replacing a chunk replaces the whole hash. `ef_runtime` sets
HNSW's search breadth. Scores are cosine similarity, 1 minus Redis's cosine distance. For a native
`aretrieve`, pass `async_client=redis.asyncio.Redis(...)`; otherwise async calls run in a thread.

## How permissions are indexed

The backend uses the same type-tagged terms as [Elasticsearch, OpenSearch](elasticsearch.md#how-permissions-are-indexed)
and [Milvus](milvus.md#how-permissions-are-indexed), so `1` and `"1"` stay apart and booleans,
floats, nested lists and objects never match. Redis TAG queries have their own syntax (braces,
`|`, `@`, `*`), and TAG matching ignores case by default, so each term is stored hex-encoded:

```python
from needtoknow import In
from needtoknow.backends.redis import filter_to_query

filter_to_query(In("allowed_groups", ["hr"]))
# '@needtoknow_terms:{616c6c6f7765645f67726f7570731f733a6872}'
```

A hex string holds only `0-9` and `a-f`, so no group name or user id can add query syntax, and
`"HR"` and `"hr"` stay different values. CI checks both on a real server. `And` becomes an
intersection and `Or` a union of TAG conditions, and an `In` with no values matches nothing without
a call to Redis. The metadata is stored as JSON and returned exactly as written, so the
retriever's re-check sees the true permissions.

## Hashes written by other tools

A hash without `needtoknow_terms` is invisible to a pre-filtered search, though post-filtering
still finds it and checks its metadata. Fill the field in your ingestion pipeline:

```python
from needtoknow.backends.redis import permission_terms

client.hset(key, mapping={"needtoknow_terms": permission_terms(metadata)})
```

## Keyword search and hybrid search

`mode="keyword"` searches the chunks' text with BM25 under the same permission filter. The index
must hold the text: create it with `content_field=`, which keeps every word (the index has no
stop words). One index then serves both halves of [hybrid search](../hybrid.md):

```python
from needtoknow import RankFusion

create_index(client, "chunks", dim=768, content_field="content")
vector = RedisBackend(client, "chunks", embed=embed)
keyword = RedisBackend(client, "chunks", mode="keyword")  # needs no embed
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

The query is split into words (letters, digits and underscores), and each word is sent as a query
parameter rather than written into the query, so no query text can add syntax: the permission
condition always applies. A query keeps its first 64 distinct words, and any of them may match.
Scores are Redis's BM25 (`BM25STD`), higher for better matches. A keyword backend without `embed`
writes chunks with no vector, which keyword search finds and vector search does not.

An existing index can gain the text field with `FT.ALTER chunks SCHEMA ADD content TEXT`, and Redis
indexes the stored hashes in the background. An index created by an earlier needtoknow keeps Redis's
default stop words, so common English words in a query match nothing there. CI runs the conformance
kit against Redis 8.10.2 in keyword mode and fused with vector search.
