# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). From 1.0 on, the public API is stable.

## [Unreleased]

## [1.2.1] - 2026-10-19

The security fix of 1.3.1, backported to 1.2 as the security policy promises for the previous minor
release. Upgrade if you use `CachingAuthorizer`, and above all if you call `invalidate()` to revoke
access; or move to 1.3.1.

### Security

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

[Unreleased]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.0.0rc1...v1.0.0
[1.0.0rc1]: https://github.com/muzakkirhussain011/needtoknow/releases/tag/v1.0.0rc1
