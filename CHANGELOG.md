# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). From 1.0 on, the public API is stable.

## [Unreleased]

### Added

- `CassandraBackend` (the `cassandra` extra): vector search on Apache Cassandra 5.0 with Storage-Attached
  Indexes. The filter becomes an OR of ANDs on type-tagged terms in an indexed `set<text>`; each AND
  is one ANN query with bound `CONTAINS` parameters, run concurrently, and the best `k` are kept.
  `set_metadata` uses a lightweight transaction so an id that is not stored creates no row;
  `create_table` creates the table and indexes. Tested with the conformance kit against Cassandra
  5.0.9. Writes go 4 at a time (`write_concurrency`), as Cassandra 5.0's vector index accepts few
  concurrent writes, and a refused write is retried once.
- `VespaBackend` (the `vespa` extra): nearest-neighbor or BM25 search on Vespa through its HTTP APIs,
  with the permission filter in the YQL query as conditions on hex-encoded, type-tagged terms, so
  Vespa applies it inside the search and no metadata value can change the query. HNSW or exact
  search; `schema(dim)` writes the schema and `examples/vespa` is a complete application package.
  Tested with the conformance kit against Vespa 8.763.13, in vector, keyword and hybrid search.

## [1.5.0] - 2026-10-23

Applications in any language can now use needtoknow through an HTTP service that takes the user
from a verified OIDC token. Redaction hides the parts of a permitted chunk a user may not see,
before a reranker or a model reads it. Ory Keto and Casbin join the permission systems, and FAISS
joins the stores, with exact pre-filtering in the process.

### Added

- `FaissBackend` (the `faiss` extra): vector search with FAISS in the process. The permission filter is
  matched against the chunks' metadata and passed to FAISS as an ID selector, so pre-filtering is
  exact. Exact search by default, or any trained index that supports ID selectors; `add`, `delete`,
  `set_metadata`, and `save` and `load` to a directory. Passes the conformance kit.
- `CasbinAuthorizer` (the `casbin` extra): decisions from the Casbin enforcer you already use, in the
  process, with `batch_enforce`. Requests are `(subject id, object, action)`, or your own for ABAC
  models; `group_subjects=True` also asks with each of the subject's groups, for allow-only models.
  An enforcer that raises or answers with anything but booleans raises `AuthorizationError`. Tested
  against the reference answer on generated corpora and with the conformance kit.
- `KetoAuthorizer`: relationship checks against Ory Keto (`POST /relation-tuples/batch/check`), with no
  extra dependency. Each chunk's document is a Keto object; `group_subjects=True` also checks the
  subject's groups as subject sets. Keto has no reverse lookup, so the retriever post-filters. A
  result carrying an error, a malformed answer or a wrong number of results raises
  `AuthorizationError`. Tested against Keto 26.2.0 and with the conformance kit.
- An HTTP service, `needtoknow.server` (the `server` extra), so applications in any language get
  permission-aware retrieval: `create_app(retriever, authenticate)` serves `POST /v1/retrieve`,
  `GET /healthz` and an OpenAPI description. The subject comes from the verified bearer token
  alone; `OIDCAuthenticator` verifies JWTs against the identity provider's published keys
  (asymmetric algorithms only, issuer, audience, expiry, an optional tenant claim). Unknown request
  fields are refused, a failed permission check answers 503 without details, and hits carry no
  metadata unless named.
- Redaction: `PermissionedRetriever(redactor=...)` hides parts of the chunks a subject may read, after
  the permission check and before a reranker or a model sees them. `SpanRedactor` hides spans listed
  in a chunk's metadata, each with its own readers; `PatternRedactor` hides text matching patterns
  unless the subject is in an exempt group. A redactor may change text and metadata but not which
  hits are returned, and one that fails makes the retrieval raise `AuthorizationError`.

## [1.4.0] - 2026-10-22

Nine of the ten stores now serve both halves of hybrid search under the permission filter: keyword
search (BM25) arrives on Qdrant, Milvus, MongoDB Atlas, Weaviate, Redis and LanceDB (Chroma has
none). Cedar joins OPA, OpenFGA, SpiceDB and Cerbos as a permission system, evaluated in the
process. Permissions can be synced from Google Drive. Operators get the cause of each failure, cache
statistics and an incident runbook; security reviewers get a threat model, the invariants with the tests that hold them, and a
compliance mapping. Several fail-closed gaps found by review are fixed, and from this release every
package carries SLSA build provenance.

**Python 3.11 or newer is required.** Python 3.10 reaches its end of life on 2026-10-31, and its
last dependency releases (Pillow 12.3 among them) no longer receive security fixes.

### Added

- `CedarAuthorizer` (the `cedar` extra): Cedar policies evaluated in the process with cedarpy. The
  principal carries the subject's id, groups and attributes, with its groups as parent entities;
  each chunk is a resource with its metadata as attributes; `entities` adds hierarchies. Cedar skips
  a policy whose evaluation errors, a `forbid` included, so a chunk whose evaluation reports an
  error is denied. No pre-filter: the retriever post-filters. Compared with the reference answer
  on generated corpora and checked with the conformance kit.
- `mode="keyword"` on the Qdrant backend: BM25 under the same `query_filter`, with no model. A
  collection created with `create_collection(..., text_search=True)` gets a sparse vector with
  Qdrant's IDF modifier; the backend writes each chunk's BM25 term weights there (the new
  `bm25_terms`), and Qdrant applies the inverse document frequency at search time. A vector
  backend given `text_search=True` writes both vectors, so one collection serves both halves of
  hybrid search. Tested with the conformance kit, alone and fused with vector search, in local
  mode and against Qdrant 1.19.2.
- `mode="keyword"` on the MongoDB backend: Atlas Search's BM25 (`$search`) on the text, under the
  same permission filter written with Atlas Search's own operators, in an index that the new
  `create_text_index` creates, so one collection serves both halves of hybrid search. A backend
  given `text_index` also waits for that index after each write. Tested with the conformance kit,
  alone and fused with vector search, against Atlas Local 8.0.30.
- `RetrievalEvent.cause`, and the span attribute `needtoknow.error.cause`: the type of the exception
  behind a failure, such as `TimeoutError` or `ConnectionRefusedError` for a permission service that
  is slow or unreachable, and `None` for an error status or a malformed answer. The HTTP authorizers
  now keep the connection error as the cause of the `AuthorizationError` they raise after retrying.
- `CachingAuthorizer.stats()`: a `CacheStats` with answers served from this process and from the
  shared cache, answers the authorizer was asked for, shared cache failures, and entries held, for
  watching the hit rate and the shared cache. The operations guide lists the new alerts.
- Documentation for security review: a threat model of the library (assets, trust boundaries,
  seventeen threats with their mitigations, remaining risks and tests), the security invariants S1 to
  S10 with the tests that hold each one, and a guide to revocation and consistency, from bounded
  staleness to strict revocation.
- Tests of tenant and context isolation against an oracle written without the library's filter
  code: tenants that share user ids, group names and document ids (including look-alike tenant
  values such as `"acme"`, `"Acme"`, `"acme"` spelt with a Cyrillic a, `1` and `"1"`) through one shared cache;
  concurrent threads and asyncio tasks for different subjects, every returned chunk checked; and a
  cancelled retrieval.
- `OpenFGAAuthorizer(consistency=...)`: `"HIGHER_CONSISTENCY"` makes OpenFGA skip its cache for every
  check and list, so a tuple just written counts at once; `"MINIMIZE_LATENCY"` is OpenFGA's default.
  Tested against OpenFGA 1.22 in each mode.
- `create_index(..., algorithm="FLAT")` on the Redis backend: an exact vector index, for indexes small
  enough to compare every vector. HNSW stays the default; the docs now say that Redis re-adds a
  chunk's vector to the graph when `set_metadata` changes it, so a small, often-changed HNSW graph
  can miss a chunk.
- `mode="keyword"` on the Weaviate backend: Weaviate's BM25 on the text, under the same permission
  filter as its vector search, with no embedding function needed, so one collection serves both
  halves of hybrid search with `RankFusion`. Tested with the conformance kit, alone and fused with
  vector search, against Weaviate 1.39.12.
- `mode="keyword"` on the Redis backend: the Query Engine's BM25 on the text, under the same
  permission filter, in an index created with `create_index(..., content_field="content")`, so one
  index serves both halves of hybrid search. Each word of the query is sent as a query parameter,
  never as query syntax. `create_index` now creates indexes without stop words. Tested with the
  conformance kit, alone and fused with vector search, against Redis 8.10.2.
- `mode="keyword"` on the Milvus backend: BM25 on a sparse vector Milvus derives from the text,
  under the same permission filter and parameters, in a collection created with
  `create_collection(..., text_search=True)`, so one collection serves both halves of hybrid
  search. Tested with the conformance kit, alone and fused with vector search, against Milvus 3.0.2.
- `mode="keyword"` on the LanceDB backend: LanceDB's full-text index (BM25) on the text, under the
  same permission filter, applied before ranking, so one table serves both halves of hybrid search.
  `create_indexes(table, content_field="content")` builds the index, keeping every word; rows
  added after it are found by a scan until `table.optimize()`. Tested with the conformance kit,
  alone and fused with vector search, with LanceDB 0.40.
- Permission sync from Google Drive. `needtoknow.sync.google_drive.GoogleDriveACL` reads who may
  read a Drive file, in My Drive or a shared drive, through your Drive v3 service, and
  `needtoknow.sync.with_access` writes the answer onto the file's chunks for
  `MetadataACLAuthorizer`, keeping their other metadata. Users and groups map to their addresses,
  a domain to the group `"domain:<domain>"` and anyone to everyone when findable in search.
  Grants that do not open the file's content grant nothing: sharing by link alone (unless
  `link_sharing=True`), deleted accounts, expired grants, view grants such as a limited-access
  folder's metadata view, inherited grants on limited-access items, and grants a shared drive's
  settings shut out (members only, domain users only). `domains=` leaves out grants outside your
  organization's domains. `needtoknow.sync.Access` is the source-neutral answer that later
  connectors return too.
- Documentation for operators and reviewers: an incident runbook (a document seen by the wrong
  person, rising `violations`, `AuthorizationError`, a failing or untrusted shared cache, an
  advisory affecting your version), a starting point for security reviewers, the configurations CI
  tests and what it does not, and a FastAPI service in `examples/fastapi_rag` that binds the caller
  from authentication. The threat model adds T17: in keyword search, BM25 statistics count chunks
  the subject may not read, so scores and order can hint at what they contain.
- CI runs the library tests on macOS and Windows as well as Linux, and fails when branch coverage
  drops below 90% for the package or 95% for the code that decides access. The release check
  installs each release from PyPI and runs the tests taken from its attested sdist.
- Benchmarks: the load benchmark checks every chunk returned under concurrent load, `--repeat`
  reports the median and range of several throughput runs, and the permissions benchmark adds a
  baseline that enforces the same permissions with PostgreSQL row-level security, as a role that
  does not bypass it.

### Fixed

- A retrieval cancelled while it ran (an asyncio task cancelled, a `KeyboardInterrupt`) produced no
  `RetrievalEvent`, though every call is meant to produce one. Observers now receive it, with
  `error="CancelledError"` (or the exception's name) and nothing returned; the exception is raised
  as before.
- `OpenFGAAuthorizer.filter_for` no longer treats a ListObjects answer that may have been cut short
  by the server's deadline as complete. OpenFGA stops at `OPENFGA_LIST_OBJECTS_DEADLINE` (3 s by
  default) and sends what it found without saying so; a call that took `list_objects_deadline`
  seconds or more (new, default 3) now gives no filter, so the retriever post-filters. Such a list
  never let anyone read more, but it could hide documents they may read while the result reported
  `complete=True`.

### Security

- The example gateway (`examples/gateway`) stores API keys as scrypt hashes (n=16384, r=8) with a
  random salt per principals file, instead of unsalted SHA-256, so a leaked principals file costs
  one scrypt per guessed key. `rampart new-key` issues random keys, `rampart key-hashing` starts a
  file with a new salt, and SHA-256 entries are refused rather than accepted as a fallback.
- `recheck=False` no longer trusts the backend completely. The authorizer is still not asked a second
  time, but each pre-filtered hit is matched against the filter in the process (no network call),
  so a backend that ignores or mistranslates the filter cannot leak; such hits are dropped and
  counted in `violations`, as with `recheck=True`. A custom backend with `recheck=False` must now
  return each chunk's metadata as stored, as the conformance kit already requires.
- `CerbosAuthorizer` refuses a subject with an attribute named `groups`. It was merged into the
  principal's attributes after the subject's own groups, so it replaced them in every decision.
- Filter parsing, and the pgvector, LanceDB and Redis translations that write query text, are
  fuzzed with ClusterFuzzLite (Atheris) on pull requests and every week. The fuzz target checks
  that parsing either round-trips or raises `ValueError`, that pgvector binds every field name and
  value as a parameter, and that LanceDB and Redis queries carry only hex-encoded terms. The test
  suite runs the same checks on generated filters.

## [1.3.3] - 2026-10-22

Closes three gaps that remained in `CachingAuthorizer.invalidate()` after 1.3.2, found by a
review of the code. Upgrade if you use a shared cache and call `invalidate()`.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/needtoknow/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- In rare interleavings, a decision revoked with `invalidate()` could still be used afterwards, for
  up to `ttl`. An `invalidate()` of everyone deleted every id's epoch, including one that a
  concurrent `invalidate()` of that id had just written, so a retrieval in flight could store its
  earlier answer where every replica accepted it. And in the process calling `invalidate()`, a read
  of the global epoch already in flight could put back the epoch it replaced, for up to
  `local_ttl`. Epochs now live outside the keys that invalidation deletes, and a read never replaces
  a newer epoch.
- An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not within
  `local_ttl` as documented: an entry accepted against a copy of the global epoch was kept for
  another `local_ttl`. Copies now expire with that epoch.

### Fixed

- Epochs are stored as hex text. Random bytes broke shared caches whose Redis client decodes
  responses (`decode_responses=True`): reads raised `UnicodeDecodeError`, so every lookup asked the
  permission service and logged a warning.
- `invalidate()` raises `TypeError` for an id that is not a string: `invalidate(42)` for the id
  `"42"` missed this process's own entries.
- A retrieval event that cannot be built is logged and dropped, instead of replacing the
  retrieval's result or error, and query fingerprints accept a lone surrogate.

### Changed

- An id's epoch has a new key in the shared cache. During a rolling upgrade, an `invalidate()` of an
  id made by one version does not reach the other version's entries: call `invalidate()` once every
  replica runs 1.3.3, or allow for `ttl`.

## [1.3.2] - 2026-10-19

Completes the 1.3.1 fix for `CachingAuthorizer.invalidate()`. Upgrade if you use a shared cache.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/needtoknow/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- With a shared cache, `invalidate()` could still leave the revoked decision in its own process for
  up to `local_ttl` (1 second by default): a retrieval running during the call could copy the old
  entry from the shared cache after the process dropped its own entries and before the new epoch
  was written. The process now drops its entries again once the new epoch is in place. Found by the
  1.3.1 revocation test under concurrent traffic; a test now forces a retrieval into that moment.

## [1.3.1] - 2026-10-19

A security fix for permission caching. Upgrade if you use `CachingAuthorizer`, and above all if
you call `invalidate()` to revoke access.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/needtoknow/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- `CachingAuthorizer.invalidate()` could be undone by a retrieval already in flight. An answer the
  permission service gave just before the revocation, to a call that finished just after it, was
  cached afterwards and used for up to `ttl` (30 seconds by default), in this process and, through
  a shared cache, in every replica. Now each process discards answers obtained by calls that began
  before an `invalidate()`, and `invalidate()` stores a new epoch in the shared cache, under which
  entries written before it are refused. Tests revoke a permission under concurrent retrievals,
  with one and three replicas, and check that no retrieval starting after `invalidate()` returns
  sees the document (other replicas within `local_ttl`).

### Changed

- `CachingAuthorizer.invalidate(subject)` takes a `Subject` or a subject id, and drops the answers
  for every subject with that id, whatever groups and attributes they came with. Before, it dropped
  only those of a subject equal in every field, so a revocation hook that knew only the user id
  (`invalidate(Subject("bob"))`) cleared nothing for `Subject("bob", ["eng"], {"tenant": "acme"})`.
- Shared cache entries have new keys and carry an epoch; entries written by earlier versions are
  not read, so the first requests after the upgrade ask the permission service. During a rolling
  upgrade, call `invalidate()` once every replica runs 1.3.1, or allow for `ttl`.

### Fixed

- `filter_from_dict` refuses a filter nested more than 100 levels deep with `ValueError`. Before,
  a deep enough document from a policy engine or a shared cache recursed until Python raised
  `RecursionError`. The shared cache treats such an entry, and any entry too deep to decode, as a
  miss.

## [1.3.0] - 2026-10-17

Retrieval as production RAG is built now, with permissions applied at every step. Hybrid search
fuses keyword and vector results under the same permission filter (BM25 on Elasticsearch and
OpenSearch, full-text search on pgvector). A reranker orders the results after the permission
check, so a cross-encoder or a hosted rerank API never sees a chunk the user may not read. Searches
can use vectors you computed, from an embedding service or another modality. AI agents get an MCP
search tool whose permissions come from the verified access token, never from the model, and
Haystack pipelines get a retriever component. Four more stores: MongoDB Atlas Vector Search,
Weaviate, Redis and LanceDB. `set_metadata` changes a document's permissions without embedding it
again, and the conformance kit checks it, like everything else, against every store.

Python 3.10 is still supported. It reaches its end of life on 2026-10-31, after this release, so
under the support policy the first minor release after that date, 1.4, drops it, not 1.3 as the
1.2.0 notes said.

### Added

- `set_metadata({chunk_id: metadata})` on every built-in backend: replaces stored chunks' metadata
  without embedding them again, so a document's permission change reaches search without the cost
  of re-embedding. The whole metadata is replaced (no old permission key survives, including where
  the store's own partial updates would merge), ids that are not stored are ignored, and it returns
  once searches see the change, waiting for MongoDB's search index. The conformance kit checks it
  for any backend that has it, against each real store in CI. Guide: "Changing a document's
  permissions" in Operations.
- Reranking after the permission check: `PermissionedRetriever(..., reranker=..., rerank_depth=50)`
  finds the top `rerank_depth` chunks the subject may read, lets a cross-encoder or hosted rerank
  API order them, and returns the first k. The reranker never sees a chunk the subject may not
  read, and can reorder, rescore or drop hits but not add one; returned hits keep their stored
  text and metadata. Any function or object with `rerank` (and optionally `arerank`) works.
  Events report `reranked` and `rerank_ms`, and OpenTelemetry records a
  `needtoknow.rerank.duration` histogram. Guide: "Reranking".
- `needtoknow.backends.lancedb`: LanceDB, the embedded vector database, with the permission filter
  applied before the vector search ranks (LanceDB's pre-filtering). Permission terms are stored
  hex-encoded in a list column and matched with `array_has_any`, and deletes match a hex-encoded id,
  so no group name, user id or document id is ever part of the SQL. `add` upserts, keeping the last
  version of a repeated id; metadata is stored as JSON and returned exactly. `create_table` and
  `create_indexes` (a `LABEL_LIST` index on the terms) set the table up. Tested with the
  conformance kit, with text and vector queries, on LanceDB 0.40. Extra: `needtoknow[lancedb]`.
- `needtoknow.mcp.add_search_tool`: a permission-aware search tool for MCP servers (MCP Python SDK
  2.3 or later), so AI assistants and agents search with the calling user's permissions. The user
  comes from the access token the server verified for the request, through your `subject_for`
  function, never from the tool's arguments; a call without a verified token is refused, and a
  fixed `Subject` serves single-user stdio servers. The read-only tool takes `query` and a bounded
  `limit`, returns structured results without metadata unless keys are named, and reports a
  permission service failure to the model without internal details. Extra: `needtoknow[mcp]`.
- `Query`: search with a vector you computed, alone or with the text. `retrieve(Query(vector=...),
  subject)` searches an image's or another model's embedding, an embedding from your own service
  or cache, or one shared by every backend in `RankFusion` (vector backends use the vector, keyword
  backends the text). Permissions apply exactly as for text. Every vector backend accepts it, and
  `embed` is now optional on all of them: build a backend without one, write with
  `add(documents, embeddings=...)` and search with vectors. The LangChain and LlamaIndex store
  wrappers search by vector, the Haystack component takes a `query_embedding` input, and the
  LlamaIndex retriever forwards a `QueryBundle`'s embedding. Audit fingerprints use the query's
  text when there is text and its vector otherwise; events never contain the vector.
  `check_backend(..., query=...)` runs the conformance kit with any query, and CI runs it with
  vector queries against every store. Guide: "Your own vectors".

- `RankFusion`: hybrid search. A backend that searches several backends with the same permission
  filter and fuses their rankings by reciprocal rank fusion, with weights, a rank constant and a
  per-backend depth; `aretrieve` searches them concurrently. A chunk two backends return with
  different metadata is left out and logged, since its permissions are uncertain. Guide: "Hybrid
  search".
- `mode="keyword"` on the pgvector backend: PostgreSQL full-text search (`websearch_to_tsquery`,
  ranked by `ts_rank_cd`) under the same permission condition, so one table serves hybrid search.
  `create_schema(..., text_search="english")` adds the full-text GIN index, and CI checks that the
  query plan uses it. Tested with the conformance kit, alone and fused with vector search.
- `mode="keyword"` on the Elasticsearch and OpenSearch backends: BM25 on the text under the same
  permission filter, with no embedding function needed. Tested with the conformance kit, alone and
  fused with vector search, against both engines.
- `needtoknow.backends.redis`: vector search on the Redis Query Engine (Redis 8 or later, or Redis
  Stack), with the permission filter in the same KNN query. Permission terms are stored
  hex-encoded in a TAG field, so no group name or user id can add query syntax and TAG matching's
  case folding never merges two values; metadata is stored as JSON and returned exactly.
  `create_index` sets up the index, and `redis.asyncio` clients give a native `aretrieve`. Tested
  with the conformance kit against Redis 8.10.2. Extra: `needtoknow[redis]`, as for the shared
  cache.
- `needtoknow.haystack.NeedToKnowRetriever`: a Haystack pipeline component over any needtoknow
  backend and authorizer. The authenticated user is a run input, so one pipeline serves every
  user, and the component refuses to run without a `Subject`. It outputs Haystack `Document`s and
  `complete`, and has `run_async`. The extra requires `haystack-ai` 2.12 or later; CI tests the
  current Haystack 3 release. Extra: `needtoknow[haystack]`.
- `needtoknow.backends.mongodb`: MongoDB Atlas Vector Search, with the permission filter as the
  `$vectorSearch` pre-filter on a type-tagged array field (a BSON filter, so values are never query
  syntax). Atlas indexes writes shortly after they happen, so `add` and `delete` wait until the
  index shows the change, including a replaced chunk's new permissions (`wait=False` skips it).
  `create_search_index` sets up the index, and `AsyncMongoClient` collections give a native
  `aretrieve`. Tested with the conformance kit against `mongodb/mongodb-atlas-local` 8.0.30. Extra:
  `needtoknow[mongodb]`.
- `needtoknow.backends.weaviate`: Weaviate, with the permission filter applied inside the vector
  search. Permissions are matched on a type-tagged `text[]` property with `field` tokenization, each
  term guarded at both ends so case and whitespace never blur two values, through the client's
  filter objects; metadata is stored as unindexed JSON and returned exactly. Document ids become
  UUIDs, with the original kept on the object. `create_collection` sets up a self-provided cosine
  vector, and `WeaviateAsyncClient` gives a native `aretrieve`. Tested with the conformance kit
  against Weaviate 1.39.12. Extra: `needtoknow[weaviate]`.

### Changed

- `SearchBackend.search` receives the query exactly as it was given to `retrieve`, a string or a
  `Query`, and its `query` parameter is typed `Any`, so backends typed for strings still satisfy
  the protocol and keep working with string queries.
- `retrieve` and `aretrieve` raise `TypeError` for a query that is neither a string nor a `Query`,
  instead of passing it to the backend.

### Fixed

- Chroma: `add` deletes and re-adds a replaced chunk, so none of its old metadata keys can remain
  and match a permission filter; it also checks that there is one embedding per document.

## [1.2.2] - 2026-10-22

Closes three gaps that remained in `CachingAuthorizer.invalidate()` after 1.2.1, found by a
review of the code. Upgrade if you use a shared cache and call `invalidate()`.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/needtoknow/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- In rare interleavings, a decision revoked with `invalidate()` could still be used afterwards, for
  up to `ttl`. An `invalidate()` of everyone deleted every id's epoch, including one that a
  concurrent `invalidate()` of that id had just written, so a retrieval in flight could store its
  earlier answer where every replica accepted it. And in the process calling `invalidate()`, a read
  of the global epoch already in flight could put back the epoch it replaced, for up to
  `local_ttl`. Epochs now live outside the keys that invalidation deletes, and a read never replaces
  a newer epoch.
- An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not within
  `local_ttl` as documented: an entry accepted against a copy of the global epoch was kept for
  another `local_ttl`. Copies now expire with that epoch.

### Fixed

- Epochs are stored as hex text. Random bytes broke shared caches whose Redis client decodes
  responses (`decode_responses=True`): reads raised `UnicodeDecodeError`, so every lookup asked the
  permission service and logged a warning.
- `invalidate()` raises `TypeError` for an id that is not a string: `invalidate(42)` for the id
  `"42"` missed this process's own entries.

### Changed

- An id's epoch has a new key in the shared cache. During a rolling upgrade, an `invalidate()` of an
  id made by one version does not reach the other version's entries: call `invalidate()` once every
  replica runs 1.2.2, or allow for `ttl`.

## [1.2.1] - 2026-10-19

The security fix of 1.3.1, backported to 1.2 as the security policy promises for the previous minor
release. Upgrade if you use `CachingAuthorizer`, and above all if you call `invalidate()` to revoke
access; or move to 1.3.1.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/needtoknow/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- `CachingAuthorizer.invalidate()` could be undone by a retrieval already in flight. An answer the
  permission service gave just before the revocation, to a call that finished just after it, was
  cached afterwards and used for up to `ttl` (30 seconds by default), in this process and, through
  a shared cache, in every replica. Now each process discards answers obtained by calls that began
  before an `invalidate()`, and `invalidate()` stores a new epoch in the shared cache, under which
  entries written before it are refused.

### Changed

- `CachingAuthorizer.invalidate(subject)` takes a `Subject` or a subject id, and drops the answers
  for every subject with that id, whatever groups and attributes they came with. Before, it dropped
  only those of a subject equal in every field.
- Shared cache entries have new keys and carry an epoch; entries written by earlier versions are
  not read, so the first requests after the upgrade ask the permission service. During a rolling
  upgrade, call `invalidate()` once every replica runs 1.2.1, or allow for `ttl`.

### Fixed

- `filter_from_dict` refuses a filter nested more than 100 levels deep with `ValueError` instead of
  recursing until `RecursionError`, and the shared cache treats such an entry as a miss.

## [1.2.0] - 2026-10-17

Breadth and scale. needtoknow now works with six more systems: the Qdrant, Milvus, Elasticsearch
and OpenSearch vector stores, and the SpiceDB and Cerbos permission systems. Authorizers combine
(`AllOf`, `AnyOf`, `TenantAuthorizer`), with scoped post-filtering for multi-tenant indexes; the
permission cache can be shared across replicas through Redis; OpenTelemetry metrics show where each
retrieval spends its time; and a subject can be in tens of thousands of groups. A conformance kit,
`needtoknow.testing`, checks your own backends and authorizers against the reference semantics, as
CI does for every built-in one. One behaviour is stricter: an authorizer answer that is not a
filter now fails closed instead of reaching the backend.

Python 3.10 is still supported. 1.2 comes out before 3.10's end of life on 2026-10-31, and the
support policy drops a version in the first minor release after that, so 1.3 will be the first
release without it, not 1.2 as the 1.1.0 notes said.

### Added

- A project logo. The documentation's front page shows it as a live 3D model (three.js, vendored, so
  the site makes no third-party requests), with a still render as the fallback when WebGL is
  unavailable or the reader prefers reduced motion; it also appears in the sidebar, as the favicon
  and at the top of the README.
- A redesigned documentation site on the Shibuya theme: a product-style front page (feature cards,
  how it works, code in tabs, integrations, measured results), a top navigation bar with menus,
  copy buttons on code, edit links on every page, and link previews. It uses each platform's own
  font and no third-party services, and its buttons meet WCAG AA contrast in light and dark mode.
- `needtoknow.testing`: a conformance kit for your own backends and authorizers.
  `check_backend` compares a backend with the reference filter semantics on generated documents and
  nested filters (exact results, `k`, ordering, unchanged metadata, `asearch`, no leaks through the
  retriever); `check_authorizer` checks that `check` and `filter_for` agree. Every built-in backend,
  and the example OPA policy against a real OPA, runs it in CI. `HashingEmbedder` gives vector stores
  deterministic embeddings without a model.
- `needtoknow.backends.qdrant`: a Qdrant backend. The filter becomes Qdrant's `query_filter`
  (`MatchAny` per value type, an empty `In` decided before the query), `create_collection` adds
  keyword payload indexes on the permission fields, points use LangChain's Qdrant layout so existing
  collections work, and `AsyncQdrantClient` gives a native `aretrieve`. Tested with the conformance kit
  against a real Qdrant server and Qdrant's local mode. Extra: `needtoknow[qdrant]`.
- `needtoknow.backends.elasticsearch` and `needtoknow.backends.opensearch`: kNN search with the
  permission filter applied during the search. Permissions are matched on one type-tagged keyword
  field (`needtoknow_terms`), because keyword fields compare strings and would otherwise match the
  integer `1` against `"1"`; metadata is stored unindexed and returned exactly, and never adds
  mappings. `create_index` sets up the mapping, `permission_terms` lets other ingestion pipelines
  write the field, and async clients give a native `aretrieve`. Tested with the conformance kit
  against Elasticsearch 9.5.5 and OpenSearch 3.9.0. Extras: `needtoknow[elasticsearch]`,
  `needtoknow[opensearch]`.
- `needtoknow.backends.milvus`: a Milvus backend, with the permission filter applied inside the
  vector search. Permissions are matched on a type-tagged array field, as on the search engines,
  and the values are passed as Milvus filter parameters, never written into the expression, so a
  group name cannot change what a filter selects. `create_collection` sets up the schema and
  indexes, searches use strong consistency by default, and `AsyncMilvusClient` gives a native
  `aretrieve`. Tested with the conformance kit against Milvus 3.0.2. Extra: `needtoknow[milvus]`.
- `needtoknow.authorizers.spicedb.SpiceDBAuthorizer`: SpiceDB over its HTTP API, with no extra
  dependency. CheckBulkPermissions in batches (answers matched by the request they echo),
  LookupResources for the pre-filter (post-filtering when the list may be truncated or is
  conditional on a caveat), the subject's groups as `group:<name>#member` subject sets, caveat
  context, a configurable consistency, and conditional answers treated as failures, not decisions.
  Tested against a real SpiceDB for random relationship graphs and with the conformance kit.
- `needtoknow.authorizers.cerbos.CerbosAuthorizer`: Cerbos over its HTTP API, with no extra
  dependency. CheckResources per chunk (metadata as resource attributes, the subject's groups and
  attributes on the principal), and pre-filters translated from PlanResources query plans: `and`,
  `or`, `hasIntersection`, `in` and `==` on resource attributes become exact filters, anything else
  falls back to post-filtering. Example policy in `examples/cerbos`. Tested against Cerbos 0.56.0.
- `AllOf` and `AnyOf` combine authorizers (each part asked only about the chunks still undecided),
  and `TenantAuthorizer` keeps every subject inside its tenant; a subject without a tenant gets
  nothing. Guide: "Combining authorizers and multi-tenancy".
- Scoped post-filtering: an authorizer may offer `scope_for(subject, action)`, a filter every
  readable chunk matches. When there is no exact filter, the retriever post-filters inside the
  scope (for example only the subject's tenant) and still checks every candidate, so a tenant with
  a small share of the index keeps its recall. `AllOf` and `AnyOf` provide scopes, and
  `CachingAuthorizer` caches them.
- `CachingAuthorizer(shared=...)`: a second cache tier shared by every process, with
  `needtoknow.caching.RedisCache` (Redis, Valkey, Dragonfly, Redis Cluster; extra
  `needtoknow[redis]`). Replicas reuse each other's answers, each keeps its own copy for at most
  `local_ttl` seconds (1 by default), and `invalidate()` reaches every replica within `local_ttl`.
  Shared entries carry their expiry time, so the revocation bound holds across replicas; key names
  are digests; values can be signed with HMAC-SHA256 (`signing_key`) so planted entries are
  ignored; and a failing shared cache is only ever a miss.
- The conformance kit also generates nested lists and objects in metadata, filters of 300 values,
  and subjects in hundreds of groups.
- `needtoknow.otel.OpenTelemetryMetrics`: retrieval metrics for dashboards and alerts. The
  `gen_ai.client.operation.duration` histogram from the OpenTelemetry GenAI conventions, retrievals
  by outcome, time spent in the backend and in the authorizer, chunks returned, examined and denied,
  and violations. Attributes have a handful of values each; subjects, queries and chunks are never
  recorded.
- `RetrievalEvent.search_ms`, `authorize_ms` and `searches`: where the time of each retrieval went,
  also on spans and in audit lines. Spans also carry the GenAI conventions' `gen_ai.operation.name`,
  `gen_ai.retrieval.top_k` and `error.type`; their name is unchanged.

### Changed

- The retriever rejects a `filter_for` (or `scope_for`) answer that is not a filter with
  `AuthorizationError`, instead of handing it to the backend.
- pgvector: an `In` with more than 100 values (a subject in many groups) is sent as two array
  parameters instead of two per value. A subject in more than about 32,000 groups used to exceed
  PostgreSQL's limit of 65,535 parameters, and retrieval failed; CI now serves one in 40,000. The
  operations guide has a section on subjects in very many groups for every backend.

### Documentation

- A production checklist: what to settle before launch, and what happens when each part of the
  system fails, with what the operator sees.
- The security model covers the shared cache's trust boundary, what a wrong `scope_for` can and
  cannot do, how each backend returns metadata, and the new authorizers.

## [1.1.2] - 2026-10-16

The README, and so the PyPI page, opens with the logo lockup, centred, with a dark version for
dark themes. The library is unchanged from 1.1.0.

## [1.1.1] - 2026-10-16

The README, and so the project's PyPI page, shows the project logo. The library is unchanged from
1.1.0.

## [1.1.0] - 2026-10-16

Hardening for production: resilient connections to remote permission services, releases that can
be verified, and the first measurements at a million chunks. The one change in behaviour is that
the authorizers no longer follow HTTP redirects. Python 3.10 reaches its end of life on 2026-10-31;
following the support policy, 1.2 will be the first release without it.

### Added

- The OPA and OpenFGA authorizers reuse HTTP connections (one per thread) instead of opening one per
  request, and replace a connection the server closed while idle.
- Transient failures are retried once by default (`retries=`): refused or reset connections,
  missing responses, HTTP 502, 503 and 504. Timeouts and other errors are not retried.
- `needtoknow.authorizers.CircuitBreaker`: pauses requests to a permission service that keeps
  failing, then probes; pass it as `circuit_breaker=`.
- `ssl_context=` on both authorizers, for your own CA or mutual TLS.
- `PgVectorBackend(ef_search=...)` sets pgvector's HNSW search depth for each search, trading
  latency for recall, without changing the setting for other users of the connection pool.
- A load benchmark (`benchmarks/load_bench.py`): a million MS MARCO passages on pgvector, recall
  and latency by selectivity, throughput with concurrent users, and the cost of OPA as the
  permission service with and without caching. CI runs a small version against real services. The
  first results are on the Benchmarks page of the documentation.
- Fault-injection tests: under any sequence of injected faults, retrieval through OPA returns
  exactly the permitted top-k or raises `AuthorizationError`.

### Security

- Releases attach the published files to the GitHub release with a Sigstore signature for each
  and a CycloneDX SBOM; SECURITY.md shows how to verify a download.
- Every GitHub Action is pinned to a commit, checkouts no longer keep credentials, CodeQL scans the
  Python code and the workflows, and OpenSSF Scorecard publishes the project's security score.

### Changed

- The authorizers no longer follow HTTP redirects; a redirect is an error, so retrieval fails
  closed.

## [1.0.0] - 2026-10-16

The first stable release. The public API in the
[API reference](https://muzakkirhussain011.github.io/needtoknow/api.html) now follows semantic
versioning. Everything in 1.0.0rc1 below is included; these are the changes since the release
candidate.

### Added

- Tested on Python 3.14. Every Python version CPython still supports is supported, and a version is
  dropped in the first minor release after its end of life (Python 3.10's is 2026-10-31).
- A release procedure for maintainers in CONTRIBUTING.

### Removed

- `RetrievalEvent.extra`, a field nothing ever set. Audit lines no longer carry an empty `"extra"`.
  Removed before 1.0 so that it is not frozen into the stable API; adding a field later is
  compatible, removing one is not.

## [1.0.0rc1] - 2026-10-16

The first release candidate for 1.0. The public API in the
[API reference](https://muzakkirhussain011.github.io/needtoknow/api.html) is what 1.0.0 will ship:
changes before then are fixes, unless testing the release candidate shows that a breaking change is
needed, in which case it will be announced in a second release candidate. Install it with
`pip install --pre needtoknow`.

### Added

- `PermissionedRetriever` with `prefilter`, `postfilter` and `auto` strategies, sync
  (`retrieve`) and async (`aretrieve`) entry points sharing one implementation.
- Failing closed: any authorizer error, wrong number of decisions or non-boolean decision raises
  `AuthorizationError` and returns nothing.
- Re-check of pre-filtered results, with `RetrievalResult.violations` counting hits that a
  backend returned despite the filter.
- A backend-neutral filter language (`In`, `And`, `Or`) with reference semantics in `matches`;
  empty `And`/`Or` are rejected because an empty conjunction would mean "allow everything".
- `MetadataACLAuthorizer` (permissions stored on each chunk) and `FunctionAuthorizer` (your own
  logic).
- `InMemoryBackend` with deterministic keyword scoring, for tests and small corpora.
- Property-based tests (Hypothesis) for the security guarantees on Python 3.10 to 3.13.
- Contributor documentation: CONTRIBUTING, GOVERNANCE, CODE_OF_CONDUCT, SECURITY.
- `needtoknow.backends.pgvector`: PostgreSQL + pgvector backend with the permission filter in the
  query as JSON containment tests (GIN-indexable), iterative HNSW scans, `create_schema`, and a
  native async search when given an async pool. Extra: `needtoknow[pgvector]`.
- `needtoknow.backends.chroma`: Chroma backend translating filters into `where` clauses. It never
  stores Chroma array metadata: in chromadb 1.5.x (local mode) a deleted collection's array
  metadata surfaces on new records in other collections and is used by `where` filters
  (chroma-core/chroma#7594). List values become
  one boolean field each, and search results are rebuilt from an exact JSON copy of the metadata,
  so the re-check always sees the true permissions. Extra: `needtoknow[chroma]`.
- `needtoknow.authorizers.opa`: Open Policy Agent authorizer using only the standard library, with
  per-chunk decisions and optional policy-provided filters; every failure fails closed. Example
  policy in `examples/opa`.
- `filter_to_dict` / `filter_from_dict` for a JSON form of filters.
- `needtoknow.authorizers.openfga`: OpenFGA authorizer over the HTTP API (no extra dependency):
  batched BatchCheck, ListObjects pre-filtering that falls back to post-filtering when the list may
  be truncated, and the subject's groups sent as contextual tuples. Tested against a real OpenFGA
  for random relationship graphs.
- `needtoknow.langchain`: `NeedToKnowRetriever` (a LangChain retriever; refuses to run without a
  subject) and `LangChainVectorStoreBackend` (any LangChain vector store, post-filtered).
  Extra: `needtoknow[langchain]`.
- `needtoknow.llamaindex`: `NeedToKnowRetriever`, `PermissionNodePostprocessor` and
  `LlamaIndexBackend`. Extra: `needtoknow[llamaindex]`.
- Runnable examples for LangChain and LlamaIndex, executed in CI.
- `CachingAuthorizer`: caches pre-filters and per-chunk decisions (keyed by chunk id and
  metadata) per subject for `ttl` seconds, with that as the documented revocation bound; errors and
  malformed answers are never cached; `invalidate()`; sync and async.
- Retrieval events: every call, including failed ones, produces a `RetrievalEvent` for
  `observers` (subject id, strategy, returned ids, denied and violation counts, duration, error
  type, optional HMAC of the query). Never query or chunk text. `JsonLinesAudit` writes them as
  JSON lines; observer failures are logged and never affect retrieval.
- `needtoknow.otel.OpenTelemetryObserver`: one span per retrieval with counts and outcomes, the
  subject id only on request. Extra: `needtoknow[otel]`.
- Documentation site (Sphinx, MyST, Furo) in `docs/`: getting started, concepts, a security model,
  operations, one page per integration, extending needtoknow, benchmarks and an API reference.
  CI builds it with warnings as errors, and every push to `main` publishes it to
  <https://muzakkirhussain011.github.io/needtoknow/>. Extra: `needtoknow[docs]`.
- Benchmark harness (`benchmarks/`): recall@k against exact ground truth, leaks, completeness and
  latency for exact search, Chroma and pgvector across permission selectivity and layouts, with a
  Kaggle notebook for real embeddings. CI runs a small version against real Chroma and PostgreSQL
  and fails on any leak.
- Conformance tests: every backend must return exactly the documents the reference semantics
  select, for random metadata and nested filters, and never leak through the retriever. CI runs
  them against real PostgreSQL, Chroma and OPA.

### Changed

- `RetrievalResult.complete` is documented precisely: it is `False` when needtoknow knows results
  may be missing, and it trusts the backend's ranking, so it cannot see results an approximate index
  failed to return. The benchmarks measure how often that happens.
- Filter values must be strings or integers, and metadata values of other types (booleans,
  floats) never match. This keeps every backend's answer identical to the reference.
- The project is now `needtoknow`, a permission-aware retrieval library (ADR 0005). The earlier
  Rampart gateway lives on as an example deployment in `examples/gateway`.

[Unreleased]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.2.0...v1.3.0
[1.2.2]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.0.0rc1...v1.0.0
[1.0.0rc1]: https://github.com/muzakkirhussain011/needtoknow/releases/tag/v1.0.0rc1
