# Apache Cassandra

[Apache Cassandra](https://cassandra.apache.org) 5.0 and newer search vectors with Storage-Attached
Indexes (SAI). `CassandraBackend` puts the permission filter in the `WHERE` clause of each ANN
query, as bound parameters, so Cassandra applies it inside the search.

```bash
pip install "wardowl[cassandra]"
```

```python
from cassandra.cluster import Cluster

from wardowl import MetadataACLAuthorizer, PermissionedRetriever
from wardowl.backends.cassandra import CassandraBackend, create_table

session = Cluster(["cassandra.internal"]).connect()
create_table(session, "rag", "chunks", dim=768)  # the table and its two SAI indexes, if missing

backend = CassandraBackend(session, "rag", "chunks", embed)
backend.add(chunks)  # or backend.add(chunks, embeddings) with vectors you computed
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
```

## How filtering works

Each row stores a tagged term for every string or integer in the chunk's metadata (the field name,
the value's type and the value) in the indexed `set<text>` column `wardowl_terms`, and the
metadata itself as JSON, returned exactly as written. A filter is rewritten as an OR of ANDs. Each
AND becomes one ANN query whose `WHERE` requires its terms with `wardowl_terms CONTAINS ?`; the
queries run concurrently and the best `k` hits of their union are returned. A subject in the groups
`eng` and `sales` costs two ANN queries.

`max_queries` (256 by default) bounds how many ANN queries one search sends; ANDs that another AND
implies are dropped first. A filter that still needs more is sent as one ANN query on the terms
every match must have, for 1,000 rows, and the backend applies the filter to those rows itself. It
never returns a chunk the filter excludes, but it can miss matches beyond those 1,000 rows, as
Cassandra's ANN search is approximate in any case.

## Production notes

- **Consistency:** reads and writes use the session's execution profile. `LOCAL_QUORUM` for both
  gives read-your-writes within a data center.
- **Permission changes:** `set_metadata` updates the metadata and terms with a lightweight
  transaction (`IF EXISTS`), so an id that is not stored never creates a row.
- **Limits:** Cassandra allows 1,000 rows per ANN query, so a search returns at most 1,000 hits.
- **Concurrent writes:** Cassandra 5.0's vector index accepts only a few writes at once per table (about
  one per core). The backend writes `write_concurrency` (4) rows at a time and sends a refused write
  once more, alone; searches run `concurrency` (32) queries at a time.
- **Similarity:** `similarity` must match the index (`create_table(..., similarity=...)`); scores
  are that function's values (`similarity_cosine` by default).
- **No keyword search:** Cassandra 5.0 has no BM25, so the backend has vector search only.
