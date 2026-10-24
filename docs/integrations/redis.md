# Redis vector search

```bash
pip install "wardowl[redis]"
```

The backend runs a KNN query on the Redis Query Engine with the permission filter as part of the
same query, so the k nearest chunks the subject may read come back however few of the index's
chunks that is. It needs Redis 8 or later, where the Query Engine is built in, or Redis Stack. CI
runs wardowl's [conformance kit](../extending.md#test-it-with-the-conformance-kit) against
Redis 8.10.2. The same `wardowl[redis]` extra serves the [shared permission cache](../operations.md#sharing-the-cache-across-processes).

```python
import redis

from wardowl.backends.redis import RedisBackend, create_index

client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=5)
create_index(client, "chunks", dim=768)  # hashes under "wardowl:", HNSW, cosine
backend = RedisBackend(client, "chunks", embed=embed)  # embed: str -> list[float]
backend.add(documents)
```

Each chunk is a hash under the prefix (`wardowl:` plus the document id) with the fields
`wardowl_id`, `content`, `metadata` (JSON), `wardowl_terms` and `embedding` (float32). A write
is searchable as soon as it returns; replacing a chunk replaces the whole hash. `ef_runtime` sets
HNSW's search breadth. Scores are cosine similarity, 1 minus Redis's cosine distance. For a native
`aretrieve`, pass `async_client=redis.asyncio.Redis(...)`; otherwise async calls run in a thread.

An index covers every hash whose key starts with its prefix. Two indexes on the default prefix
therefore index each other's chunks, and two backends that write the same chunk id overwrite one
hash. Give each index its own prefix, and pass the same prefix to its backends:

```python
create_index(client, "acme_chunks", dim=768, prefix="acme:")
acme = RedisBackend(client, "acme_chunks", embed=embed, prefix="acme:")
```

Prefixes are compared as plain strings, so end each with a separator: `t1:` and `t10:` are apart,
but every key under `t10` also starts with `t1`.

HNSW is approximate. Redis indexes a hash again whenever any of its fields changes, so `set_metadata`
removes and re-adds the chunk's vector in the graph, and a small graph that changes often can miss
a chunk; it never returns one the filter excludes. `create_index(..., algorithm="FLAT")` compares
the query with every vector instead: exact, and fast enough up to some tens of thousands of chunks.
CI checks both, exactly on `FLAT`.

**Timeouts.** By default the Query Engine answers a query that runs out of time with the results
it found so far, which look complete. In production, make it fail instead: set
`search-on-timeout fail` in the Redis 8 configuration (`FT.CONFIG SET ON_TIMEOUT FAIL` on Redis
Stack), and a timed-out search raises. A client using RESP3 (`protocol=3`) also receives a
warning with such partial results, and the backend raises `RuntimeError` when it sees one; RESP2
replies carry no warning, so with RESP2 the server setting is the only protection. On the client's
side, redis-py waits for an answer without limit by default: set `socket_connect_timeout` and
`socket_timeout`, as in the example above, so that a Redis that hangs raises instead of holding up
retrievals.

## How permissions are indexed

The backend uses the same type-tagged terms as [Elasticsearch, OpenSearch](elasticsearch.md#how-permissions-are-indexed)
and [Milvus](milvus.md#how-permissions-are-indexed), so `1` and `"1"` stay apart and booleans,
floats, nested lists and objects never match. Redis TAG queries have their own syntax (braces,
`|`, `@`, `*`), and TAG matching ignores case by default, so each term is stored hex-encoded:

```python
from wardowl import In
from wardowl.backends.redis import filter_to_query

filter_to_query(In("allowed_groups", ["hr"]))
# '@wardowl_terms:{616c6c6f7765645f67726f7570731f733a6872}'
```

A hex string holds only `0-9` and `a-f`, so no group name or user id can add query syntax, and
`"HR"` and `"hr"` stay different values. CI checks both on a real server. `And` becomes an
intersection and `Or` a union of TAG conditions, and an `In` with no values matches nothing without
a call to Redis; `filter_to_query` raises `NothingMatches` for a filter that can match nothing,
since a query without the expression matches every hash. The metadata is stored as JSON and
returned exactly as written, so the retriever's re-check sees the true permissions.

## Hashes written by other tools

A hash without `wardowl_terms` is invisible to a pre-filtered search, though post-filtering
still finds it and checks its metadata. Fill the field in your ingestion pipeline:

```python
from wardowl.backends.redis import permission_terms

client.hset(key, mapping={"wardowl_terms": permission_terms(metadata)})
```

## Keyword search and hybrid search

`mode="keyword"` searches the chunks' text with BM25 under the same permission filter. The index
must hold the text: create it with `content_field=`, which keeps every word (the index has no
stop words). One index then serves both halves of [hybrid search](../hybrid.md):

```python
from wardowl import RankFusion

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
indexes the stored hashes in the background. An index created by an earlier wardowl keeps Redis's
default stop words, so common English words in a query match nothing there. CI runs the conformance
kit against Redis 8.10.2 in keyword mode and fused with vector search.
