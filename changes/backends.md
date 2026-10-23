### Security

- `FaissBackend.add` keeps only the last document of an id repeated in one call. Each copy used to
  get its own slot, and the earlier copies stayed in the index, out of reach of `delete` and
  `set_metadata`, so searches kept returning them with their old metadata after a revocation.
- `QdrantBackend` and `WeaviateBackend` derive every point or object id from the SHA-256 of the
  chunk id (`point_id`), a chunk id that is already a UUID included. A UUID used to be kept as it
  was, so another spelling of it (upper case, braces, `urn:uuid:`), or an id chosen to equal the
  UUID derived from another, landed on the same point. `set_metadata` and `delete` now act only on a
  point whose stored `wardowl_id` is the id asked for (Qdrant: a filter on the payload; Weaviate: the
  property, checked before each update and required by each delete), so they never change or remove
  a chunk that holds another id. A point or object written by another tool, without `wardowl_id`, is
  reached by its UUID written exactly as search results report it. Collections must be indexed
  again, as the rename already requires.
- `PineconeBackend` stores a chunk id that starts with `sha256-` under its SHA-256, like ids Pinecone
  cannot hold, so no chunk id can share a record with another's hashed id. `set_metadata` and
  `delete` fetch the records first (`GET /vectors/fetch`) and leave alone any record that holds
  another chunk id.
- `MilvusBackend.delete` and `set_metadata` select rows with `id in {ids}` and the ids as a filter
  parameter. pymilvus's `get(ids=...)` and `delete(ids=...)` write string ids into the expression
  between quotes, so an id such as `a','b` selected the chunks `a` and `b` as well.
- `set_metadata` never writes a whole chunk back. Milvus updates only the metadata and terms with a
  partial update (Milvus 2.6.2 or newer), LanceDB with an update-only merge of those columns, Chroma
  with an update (removing stale list fields in the same call), and Qdrant sets only the payload's
  metadata key. Before, a chunk deleted while `set_metadata` ran could be inserted again with its
  old text, and a concurrent `add` could have its new text replaced by the old.
- `CassandraBackend.set_metadata` uses plain writes, like `add` and `delete`, instead of a
  lightweight transaction. Cassandra orders writes to a row by timestamp, and the transaction's
  Paxos timestamp does not order with the client timestamps of plain writes, so a permission change
  could be applied over a later one. Ids are looked up first, so an id that is not stored still
  creates no row; a chunk deleted between the lookup and the update is left without text or vector,
  which an ANN search never returns.
- `PgVectorBackend` matches an integer value only where the stored number reads as that integer, as
  the reference semantics do: jsonb compares numbers by value, so `In("level", [1])` used to match
  `{"level": 1.0}`. Metadata keys that are not strings are refused, since JSON turned the key `1`
  into `"1"`, which a filter on the field `"1"` then matched.
- Chroma's `filter_to_where` refuses field names that contain `::` or equal `wardowl_metadata`,
  which addressed the stored list fields or the JSON copy rather than a metadata field.

### Changed

- The public filter translators raise the new `wardowl.backends.NothingMatches` (a `ValueError`) for
  a filter that can match nothing, instead of returning `None` or `{}`, which store clients read as
  no filter at all: `filter_to_query` (Elasticsearch, OpenSearch, Redis), `filter_to_where`
  (Chroma), `filter_to_qdrant`, `filter_to_mongo`, `filter_to_search`, `filter_to_expression`
  (Milvus), `filter_to_weaviate`, `filter_to_sql` (LanceDB), `filter_to_yql` and
  `filter_to_pinecone`. Chroma's `NEVER` constant is removed. The backends still return no hits for
  such a filter without asking the store.
- A search with `k` above what the store returns in one query raises `ValueError` instead of
  returning that many hits as if no more matched: 10,000 for Elasticsearch, OpenSearch, MongoDB
  Atlas Vector Search and Pinecone, and 1,000 for Cassandra.
- `PineconeBackend` waits 30 seconds by default (`wait=30.0`, was `None`) for a search to see each
  write, so a revoked permission or a deleted chunk is out of searches when the call returns.
  `wait=None` keeps the old behaviour.

### Fixed

- `VespaBackend.search` raises when the result does not cover every document (`coverage.full`
  false, or `coverage.degraded` set), instead of returning partial results as complete.
- `RedisBackend.search` raises when a RESP3 reply carries the Query Engine's timeout warning, which
  marks partial results. The Redis guide now recommends `search-on-timeout fail`, the only
  protection for RESP2 clients.
