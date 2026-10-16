# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). From 1.0 on, the public API is stable.

## [Unreleased]

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

[Unreleased]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/muzakkirhussain011/needtoknow/compare/v1.0.0rc1...v1.0.0
[1.0.0rc1]: https://github.com/muzakkirhussain011/needtoknow/releases/tag/v1.0.0rc1
