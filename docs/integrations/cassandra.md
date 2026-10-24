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
session.execute(
    "CREATE KEYSPACE IF NOT EXISTS rag "
    "WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3}"  # your data centers
)
create_table(session, "rag", "chunks", dim=768)  # the table and its two SAI indexes, if missing

backend = CassandraBackend(session, "rag", "chunks", embed)
backend.add(chunks)  # or backend.add(chunks, embeddings) with vectors you computed
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
```

`create_table` creates the table and its indexes, not the keyspace, so create the keyspace first,
with the replication your cluster needs: `NetworkTopologyStrategy` with a replica count for each
data center in production, or `{'class': 'SimpleStrategy', 'replication_factor': 1}` for a single
test node, as CI uses. Keyspace and table names are plain lowercase CQL names.

## How filtering works

Each row stores a tagged term for every string or integer in the chunk's metadata (the field name,
the value's type and the value) in the indexed `set<text>` column `wardowl_terms`, and the
metadata itself as JSON, returned exactly as written. A filter is rewritten as an OR of ANDs. Each
AND becomes one ANN query whose `WHERE` requires its terms with `wardowl_terms CONTAINS ?`; the
queries run concurrently and the best `k` hits of their union are returned. A subject in the groups
`eng` and `sales` costs two ANN queries.

The vector index is approximate, so a search can now and then return fewer than `k` hits when more
match. The filter is exact: no hit is ever a chunk the filter excludes.

`max_queries` (256 by default) bounds how many ANN queries one search sends; ANDs that another AND
implies are dropped first.

### Filters that need more than `max_queries` queries

A filter that still needs more ANDs than `max_queries` is sent as one ANN query on the terms every
match must have, for 1,000 rows, and the backend applies the filter to those rows itself. With
`MetadataACLAuthorizer` this happens for a subject in more than 254 groups at the default (one
query for the user, one per group and one for `"*"`). An `And` multiplies the counts: two tenants
and 200 groups make 404.

The search never returns a chunk the filter excludes, but it can return **fewer than `k` hits, or
none, though more chunks match**, and nothing says so: the retriever reports the result as
complete. That happens when more than 1,000 rows hold the terms every match needs and fewer than
`k` of the 1,000 nearest pass the filter. An `Or` of users and groups needs no term in common, so
the query reads the 1,000 nearest rows of the whole table; on a large table where the subject may
read a small part, few of them pass.

To avoid it:

- Raise `max_queries` above the largest number of ANDs your filters need. Each AND is one ANN
  query, sent `concurrency` (32) at a time.
- Narrow the group lists: give the authorizer only the groups that grant access to chunks, not
  every group in the directory.
- Put a term every match must have in the filter, such as a tenant: in
  `And(In("tenant", ["acme"]), Or(...))` the one query requires the tenant's term, so it reads the
  1,000 nearest rows of that tenant rather than of the whole table.

## Production notes

- **Consistency:** reads and writes use the session's execution profile. `LOCAL_QUORUM` for both
  gives read-your-writes within a data center.
- **Permission changes:** `set_metadata` looks the ids up and updates the metadata and terms of
  the rows found, so an id that is not stored creates no row. Its writes are plain writes, like
  those of `add` and `delete`, and use no lightweight transaction: Cassandra orders writes to a row
  by timestamp, and a lightweight transaction takes its timestamp from Paxos rather than from the
  client, so mixing the two could apply an older permission over a newer one. A chunk deleted
  between the lookup and the update is left as a row with metadata and terms but no text and no
  vector, which an ANN search never returns; a later `add` of that id replaces it.
- **Limits:** Cassandra allows 1,000 rows per ANN query, so a search with `k` above 1,000 raises
  `ValueError` rather than return 1,000 hits as if no more matched.
- **Concurrent writes:** Cassandra 5.0's vector index accepts only a few writes at once per table (about
  one per core). The backend writes `write_concurrency` (4) rows at a time and sends a refused write
  once more, alone; searches run `concurrency` (32) queries at a time.
- **Similarity:** `similarity` must match the index (`create_table(..., similarity=...)`); scores
  are that function's values (`similarity_cosine` by default).
- **No keyword search:** Cassandra 5.0 has no BM25, so the backend has vector search only.
