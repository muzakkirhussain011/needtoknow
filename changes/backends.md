### Security

- `PgVectorBackend` writes a float without a fraction with one (`10000000000000000.0`), in lists and
  nested objects too. `json.dumps` writes every float of magnitude 1e16 or more with an exponent
  (`1.2345678901234568e+16`), which jsonb stores as an integer, so the value came back as an int and
  `In("owner", [12345678901234568])` matched it, in SQL and in the retriever's re-check, though
  `matches` does not. A pandas integer column with a missing value holds such floats. Rows written
  before keep the integer until they are written again with `add` or `set_metadata`.
- `FaissBackend` keeps its own copy of each chunk's metadata and returns copies, as
  `InMemoryBackend` does. `add` kept the caller's `Document`, `set_metadata` a shallow copy of the
  dict, and `search` returned the stored dict, so
  `hits[0].metadata["allowed_groups"].append("eng")`, or a change to a dict after passing it in,
  granted `eng` from the next search.
- `ChromaBackend.search` drops a hit whose metadata, rebuilt from the JSON copy, the filter does not
  match. `set_metadata` reads the stored fields and updates them in two calls, so an `add` between
  them could leave list fields the update did not remove, and the `where` filter matched the chunk
  on a group its metadata no longer granted.
- `WeaviateBackend.set_metadata` and `delete` count an object as written by another tool only when
  it has no string `wardowl_id`, as search results do, not when the id is empty. A chunk stored with
  the id `""` counted as another tool's, so a call naming its object's UUID as the chunk id changed
  or removed it.
- `VespaBackend` refuses the chunk ids `.` and `..` with `ValueError` in `add`, `set_metadata` and
  `delete`, before sending any request. httpx drops such a path segment, so the request went to
  another path, where a 404 passed for "not stored": a `delete` or `set_metadata` of `..` changed
  nothing and returned as if done. Vespa's `/document/v1` refuses these ids percent-encoded too.

### Fixed

- `ElasticsearchBackend` and `OpenSearchBackend` send `allow_partial_search_results=false` with
  every search, and raise `RuntimeError` for an answer that reports a failed shard
  (`_shards.failed` above 0) or `timed_out: true`. Such an answer is HTTP 200 and carries only the
  other shards' hits, which were returned as complete.

### Documentation

- The Cassandra page says when a search falls back to one ANN query filtered in the process (a
  filter that needs more than `max_queries` ANDs, such as a subject in more than 254 groups at the
  default), that the search can then return fewer than `k` hits, or none, though more chunks match,
  with nothing in the result saying so, and how to avoid it: raise `max_queries`, send fewer groups,
  or put a term every match needs, such as a tenant, in the filter.
- The Cassandra page says the vector index is approximate, so a search can now and then return
  fewer than `k` hits when more match, and the conformance tests check Cassandra as an approximate
  index. The filter stays exact.
