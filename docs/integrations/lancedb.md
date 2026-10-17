# LanceDB

```bash
pip install "needtoknow[lancedb]"
```

LanceDB is an embedded vector database: it runs inside your process, on a local directory or on
object storage, with no server to operate. The backend runs LanceDB's vector search with the
permission filter applied before ranking (LanceDB's pre-filtering), so the k nearest chunks the
subject may read come back however few of the table's chunks that is. CI runs needtoknow's
[conformance kit](../extending.md#test-it-with-the-conformance-kit) against LanceDB 0.40.

```python
import lancedb

from needtoknow.backends.lancedb import LanceDBBackend, create_indexes, create_table

db = lancedb.connect("data/chunks")  # or an object storage URI
table = create_table(db, "chunks", dim=768)
backend = LanceDBBackend(table, embed=embed)  # embed: str -> list[float]
backend.add(documents)
create_indexes(table)  # scalar indexes on the permission terms and ids, once data is loaded
```

Each chunk is a row with the columns `id`, `needtoknow_key` (the id, hex-encoded), `content`,
`metadata` (JSON), `needtoknow_terms` and `vector` (float32). `add` upserts by id, and a write is
searchable as soon as it returns; when one call adds the same id twice, the last version wins.
Scores are cosine similarity, 1 minus LanceDB's cosine distance. Without an ANN index, LanceDB
searches every row exactly; add one as for any LanceDB table, and tune it with `nprobes` and
`refine_factor`. Async calls run in a thread. Without `embed`, write with
`add(documents, embeddings=...)` and search with [`Query(vector=...)`](../vectors.md).

## How permissions are indexed

The backend uses the same type-tagged terms as [Elasticsearch, OpenSearch](elasticsearch.md#how-permissions-are-indexed)
and [Redis](redis.md#how-permissions-are-indexed), so `1` and `"1"` stay apart and booleans,
floats, nested lists and objects never match. LanceDB filters are SQL, so each term is stored
hex-encoded in a list column, and a filter becomes `array_has_any` over hex strings:

```python
from needtoknow import In
from needtoknow.backends.lancedb import filter_to_sql

filter_to_sql(In("allowed_groups", ["hr"]))
# "array_has_any(needtoknow_terms, ['616c6c6f7765645f67726f7570731f733a6872'])"
```

A hex string holds only `0-9` and `a-f`, so no group name or user id is ever part of the SQL; CI
checks that with quotes and SQL fragments as group names. Deletes match the hex-encoded id column
for the same reason. `And` and `Or` become `AND` and `OR`, and an `In` with no values matches
nothing without a query. `create_indexes` adds a `LABEL_LIST` index on the terms, the index type
LanceDB uses for `array_has_any`; rows added later are found before they are indexed. The metadata
is stored as JSON and returned exactly as written, so the retriever's re-check sees the true
permissions.

## Rows written by other tools

A row with no `needtoknow_terms` is invisible to a pre-filtered search, though post-filtering still
finds it and checks its metadata. Fill the column in your ingestion pipeline:

```python
from needtoknow.backends.lancedb import permission_terms

row["needtoknow_terms"] = permission_terms(metadata)
```

A row whose `metadata` is not a JSON object carries no permissions, so the re-check denies it.

## Keyword search and hybrid search

`mode="keyword"` searches the chunks' text with LanceDB's full-text index (BM25), under the same
permission filter, applied before the results are ranked. Build the index with
`create_indexes(table, content_field="content")`, which keeps every word (no stop words are
removed). One table then serves both halves of [hybrid search](../hybrid.md):

```python
from needtoknow import RankFusion
from needtoknow.backends.lancedb import create_indexes

create_indexes(table, content_field="content")
vector = LanceDBBackend(table, embed=embed)
keyword = LanceDBBackend(table, mode="keyword")  # needs no embed
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

Rows added after the index was built are found at once, by a scan of the rows the index does not
hold yet; `table.optimize()` adds them to the index, which keeps keyword search fast as the table
grows. Scores are LanceDB's BM25, higher for better matches. A keyword backend without `embed`
writes chunks with no vector, which keyword search finds and vector search does not. CI runs the
conformance kit in keyword mode and fused with vector search, with the index built before and
after the rows arrive.
