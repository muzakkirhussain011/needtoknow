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
