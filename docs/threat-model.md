# Threat model

This is the threat model of the wardowl library: what it protects, where the trust boundaries
are, what can go wrong at each, and what stops it. The example gateway in `examples/gateway` has
its own threat model, for a deployed service. [Security model](security.md) states the guarantees
and the invariants (S1 to S10) these threats refer to; [Revocation and consistency](revocation.md)
covers how quickly permission changes take effect.

## What is protected

| Asset | Why it matters |
|---|---|
| Document chunks: text and metadata | The confidential content itself. Metadata can name people, groups and projects. |
| Embeddings | Can be inverted to approximate the text they came from. |
| Permission data: chunk ACLs, policies, relationship tuples | Decide who reads what. |
| Cached decisions and filters | A decision that outlives its permission grants access after revocation. |
| Subjects: user ids, groups, tenant and other attributes | Who is asking. A wrong subject is a wrong answer. |
| Queries | Reveal what people look for; audit events must not store them. |
| Credentials for stores, permission services and Redis | Give direct access around wardowl. |

## Trust boundaries

```text
 end user / agent ──▶ your application ──▶ wardowl ──▶ vector store
 (untrusted)          authenticates,        (in process)   (trusted to search,
                      builds the Subject         │          checked for filtering)
                                                 ├──▶ permission service (OPA, OpenFGA, SpiceDB, Cerbos)
                                                 │    (source of truth for decisions)
                                                 ├──▶ shared cache (Redis), trusted only when signed
                                                 ├──▶ reranker, then the language model
                                                 │    (sees only permitted chunks)
                                                 └──▶ observers: audit log, OpenTelemetry
                                                      (see counts and ids, never content)
```

- **User or agent to your application.** Untrusted. wardowl does not authenticate anyone: your
  application builds the `Subject` from a verified session or token. A language model is on the
  untrusted side; it chooses what to search for, never whose permissions apply.
- **wardowl to the vector store.** The store is trusted to search and to return each chunk's
  metadata as stored. It is not trusted to apply the permission filter correctly: every pre-filtered
  hit is checked again.
- **wardowl to the permission service.** Trusted for its answers, never for their shape: a
  missing, partial, malformed or late answer fails closed.
- **wardowl to the shared cache.** Trusted with decisions only when values are signed.
- **wardowl to the reranker and the model.** They receive permitted chunks only, after the check.
- **Ingestion and permission sync.** Outside the request path: whatever writes permissions onto
  chunks (`set_metadata`, the Google Drive sync, your pipeline) is trusted, and its delay is a
  revocation delay.

## Threats

Severity is the impact if the threat were realised with no mitigation: **critical** (any user reads
another tenant's or another user's confidential data at will), **high** (confidential data reaches
someone not entitled to it under some conditions), **medium** (data hidden from those entitled to
it, staleness within a stated bound, or metadata disclosure), **low** (hints, cost).

| Id | Threat | Severity | Mitigation | Remaining risk | Tests |
|---|---|---|---|---|---|
| T1 | Subject impersonation: the client or a model chooses whose permissions apply | Critical | No API takes the subject from the client. The MCP tool reads it from the verified access token; LangChain and LlamaIndex bind it per session; Haystack's `WardowlRetriever` keeps `subject` out of a tool's parameters and refuses the JSON a model sends for it anyway, and `BoundWardowlRetriever` reads it from a function. | Your application must build the `Subject` from authentication; wardowl cannot tell. | `test_mcp.py`, `test_frameworks.py`, `test_haystack.py` |
| T2 | The store ignores or mistranslates the filter | High | Every pre-filtered hit is checked again with the authorizer (`recheck=True`) or, with `recheck=False`, against the filter locally; stray hits are dropped and counted as `violations`. The conformance kit checks every store. | With `recheck=False`, a filter broader than the authorizer's own check goes unnoticed. | S1, S5 |
| T3 | The permission service fails, times out or is unreachable | High | `AuthorizationError`; no fallback to unfiltered results. Retries only for transient failures, a circuit breaker for a failing service. | Availability: retrieval stops while the service is down. | S2 |
| T4 | A malformed, partial or misordered answer: wrong count, non-boolean, an unknown permissionship, a truncated list | High | Decisions are validated for count and exact `bool` type; SpiceDB answers are matched by request, not position; a list that reached its limit or the deadline gives no filter. | An authorizer that returns well-formed wrong answers is trusted (it is the source of truth). | S2, S7 |
| T5 | Tenant crossover: shared user ids, group names or document ids, or a cached decision for another tenant | Critical | `AllOf(TenantAuthorizer(), ...)`; a subject without a usable tenant gets nothing; cache keys include the whole subject, its tenant attribute too. | Resource ids in a shared OpenFGA or SpiceDB store must be tenant-qualified (`acme/handbook`); a tenant taken from the client instead of authentication defeats it. | S3, S4 |
| T6 | Stale permissions from caching | Medium | `ttl` bounds every cached decision, across replicas too; `invalidate()` drops decisions at once, including answers still in flight, and reaches other replicas within `local_ttl`. | Up to `ttl` after a revocation nobody reported; the permission service's own staleness adds to it. | S9 |
| T7 | Stale permissions on the chunks (metadata ACLs) | High | `set_metadata` replaces a chunk's permissions in place; the Google Drive sync writes current sharing. | Until the chunks are updated, the old permissions apply. | Conformance kit (`set_metadata`), `test_sync.py` |
| T8 | A shared cache written by someone else | High | `RedisCache(signing_key=...)`: an HMAC over each key and value; anything unsigned, altered or moved is ignored. A failing cache is a miss. | Without a signing key, anyone who can write to that Redis can plant decisions. With one, an old signed entry put back can last until its own expiry, at most `ttl`. | `test_shared_cache.py`, S9 |
| T9 | Two differently configured authorizers sharing one cache namespace | High | A shared cache requires a `namespace`; within one process, a second kind of authorizer under a Redis client and namespace already in use raises `ValueError`. | Two configurations of one authorizer type, or two processes, given the same namespace still share answers: the names are yours to keep apart. | `test_shared_cache.py` |
| T10 | Injection through field names, values or query text into store queries | High | Values are bound as parameters or sent as hex-encoded terms; field names are validated; filter parsing is depth-limited. Fuzzed with ClusterFuzzLite. | | `test_fuzz_properties.py`, `test_translators.py` |
| T11 | Content the subject may not read reaches a reranker, the model, a tool or a callback | High | The reranker runs after the check and can only reorder, score or drop; its output is mapped back to the chunks it was given. The MCP tool and the LangChain, LlamaIndex and Haystack retrievers return no metadata unless named. | Framework callbacks and traces (LangSmith, OpenTelemetry exporters you add) receive the permitted text; they are your systems. | S6 |
| T12 | Disclosure through errors, logs and telemetry | Medium | Events carry ids and counts, never query or chunk text; the query only as an HMAC. Spans omit the subject unless asked. The MCP tool reports failures without their text. | `AuthorizationError` messages can name services and resource ids, and `examined`, `denied` and latency hint that matching forbidden documents exist: keep them in your logs, away from end users. | S10 |
| T13 | Decisions carried over between concurrent requests, threads or tasks | High | The retriever keeps no per-request state between calls; the cache is keyed by subject and action, and its updates are locked. | | S4 |
| T14 | Exhaustion: deeply nested filters, subjects in thousands of groups, a slow permission service | Medium | Filters nest at most 100 deep; pgvector uses array parameters above a threshold; timeouts, a circuit breaker and `max_candidates` bound the work. | A subject in very many groups makes every query larger. | `test_filters.py`, `test_fuzz_properties.py` |
| T15 | A compromised dependency or release | High | CI and the release install everything from `uv.lock` by hash; releases are built in GitHub Actions, published with trusted publishing, signed with Sigstore and carry SLSA provenance and an SBOM. OpenSSF Scorecard runs on every change. | The optional extras pull each store's client and its dependencies. Advisories with no fixed release are listed with reasons in `osv-scanner.toml`. | Scorecard, `verify-release.yml` |
| T16 | An incomplete result presented as complete | Medium | `complete=False` when post-filtering stopped at `max_candidates`, when the re-check removed hits, or when the authorizer's list may have been truncated (no filter is used). | An approximate index can miss permitted chunks without anyone knowing; tune the index and measure recall. | S7 |
| T17 | Ranking statistics computed over chunks the subject may not read | Medium | Only permitted chunks are returned, whatever the ranking. Vector search scores each chunk on its own, and `RankFusion` combines ranks, not scores. | In keyword search (`mode="keyword"`, so also hybrid search), the store's BM25 weighs terms by statistics over the whole index or collection, forbidden chunks included (on every backend except PostgreSQL, whose `ts_rank_cd` uses no such statistics), so the order and scores of permitted chunks shift with what the forbidden ones contain. Someone who can send many queries and see scores or compare orders can infer whether a rare term appears in documents they may not read ([Büttcher and Clarke, 2005](https://www.usenix.org/conference/fast-05/security-model-full-text-file-system-search-multi-user-environments)). Keep scores away from end users (the MCP tool and the LangChain retriever return them), or give each tenant or permission domain its own keyword index. | |

## Out of scope

These are real risks for a RAG system; wardowl does not address them:

- **Authentication.** Verifying users, tokens and sessions.
- **The model's output.** A model can repeat, summarise or be steered into misusing a chunk the
  user was allowed to read.
- **Prompt injection in permitted content.** A readable chunk can carry instructions for the model.
- **The permission policy itself.** If the policy or the tuples grant too much, wardowl enforces
  too much.
- **Infrastructure.** TLS, network isolation and access control for the stores, the permission
  service and Redis.

## Reviewing this model

The model is reviewed with every change that adds a backend, an authorizer, a cache or an
integration, and the invariants table in [Security model](security.md) is the checklist. It has been
reviewed internally only: no independent security audit has been carried out yet.
