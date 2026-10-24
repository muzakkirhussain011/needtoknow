# For security reviewers

Where to start when reviewing wardowl: what to read, which code decides what, the contracts it
relies on, the APIs worth a second look, the evidence, and how to reproduce it. **No independent
security audit has been carried out yet.** Everything below is the project's own work, which a
review should check rather than take on trust.

## Scope

The `wardowl` package: the retriever, the filter language, the authorizers, the caching layer,
the store backends and the framework adapters. The example gateway in `examples/gateway` is a
separate deployment with its
[own threat model](https://github.com/muzakkirhussain011/wardowl/blob/main/examples/gateway/threat-model.md).

## Read first

1. [Security model](security.md): what is guaranteed, what is assumed, and the invariants S1 to S10
   with the tests that hold each one.
2. [Threat model](threat-model.md): assets, trust boundaries, seventeen threats, remaining risks.
3. [Concepts](concepts.md): pre-filtering, post-filtering, the re-check, failing closed.
4. [Revocation and consistency](revocation.md): where staleness comes from, and the two ways to
   configure it.
5. [Operations](operations.md): the cache, the shared cache, retries and the circuit breaker, audit
   events, alerts.
6. [Supported configurations](support.md): what CI tests, and what it does not.

## The code, by what it decides

| Module | Decides | Read for |
|---|---|---|
| `wardowl/retriever.py` | Which strategy runs, the pre-filter and its re-check, post-filtering and how far it widens, reranking, the validation of every answer, the events | Every result passes through it. One plan serves `retrieve` and `aretrieve`. |
| `wardowl/filters.py` | The filter language and `matches`, its reference semantics | Every store translation must agree with `matches`; parsing is depth-limited. |
| `wardowl/authz.py` | `MetadataACLAuthorizer`, `TenantAuthorizer`, `AllOf`, `AnyOf` | Composition, and when a combined filter or scope exists. |
| `wardowl/caching.py` | Cached decisions, the shared cache, signing, epochs, `invalidate()` | Revocation and replay. |
| `wardowl/authorizers/` | Requests to OPA, OpenFGA, SpiceDB and Cerbos, and the parsing of their answers | Partial, malformed and truncated answers. |
| `wardowl/backends/` | Each store's translation of a filter into its query | Injection, type confusion, a filter made broader. |
| `wardowl/mcp.py`, `langchain.py`, `llamaindex.py`, `haystack.py` | How each framework binds the subject | Impersonation by a client or a model. |

## The contracts

- **A backend** returns hits best first, applies a filter exactly as `matches` defines it, returns
  "nothing can match" as no results (never as no filter), and returns each chunk's metadata as
  stored. `wardowl.testing.check_backend` checks all four, and every built-in backend passes it
  against its real store in CI.
- **An authorizer**'s `check` returns one real `bool` per hit, in order; `filter_for` returns a filter
  that selects exactly what `check` allows, or None; `scope_for`, when present, returns a condition
  every readable chunk meets. `wardowl.testing.check_authorizer` checks the first two agree. Any
  failure raises, and the retriever then returns nothing.
- **The subject** comes from your authentication. wardowl cannot verify it.

## APIs that deserve a second look

- `PermissionedRetriever(recheck=False)`: pre-filtered hits are matched against the filter locally
  but not decided by the authorizer again, so a filter broader than the authorizer's own check goes
  unnoticed.
- `CachingAuthorizer(shared=...)`: the `namespace` is required, and a process refuses a second kind
  of authorizer under one it already uses, but two processes configured differently under the same
  Redis and namespace still share answers.
- `SpanRedactor(required=False)`: a chunk without its spans field is shown in full.
- `PineconeBackend(wait=None)`: a write returns before searches see it, so a revoked chunk can
  still be returned for a few seconds.
- `RedisCache` without `signing_key`: whoever can write to that Redis can plant decisions.
- `FunctionAuthorizer` with a `filter_for` broader than its `check`: the re-check catches it (as
  violations); `recheck=False` does not.
- `OpenFGAAuthorizer(contextual_groups=True)` and `SpiceDBAuthorizer(group_subjects=True)`: the
  subject's groups are trusted as given. With SpiceDB, each group is also asked about on its own,
  which is wrong for a permission that excludes (`viewer - banned`) or intersects; the default,
  `group_subjects=False`, leaves memberships to SpiceDB.
- `SpiceDBAuthorizer`'s default consistency, `minimizeLatency`, and OpenFGA's default,
  `MINIMIZE_LATENCY`: answers can trail a write.
- `Subject.attributes`: part of every cache key, and the tenant's source for `TenantAuthorizer`; it
  must come from authentication like the rest of the subject.

## The evidence

- **Tests.** Over 500, many of them Hypothesis property tests, compared with oracles written without
  the library's filter code. CI runs them on Python 3.11 to 3.14 on Linux, the library tests on
  macOS and Windows too, and the suites against 12 vector stores and 5 permission services running
  as real servers, Pinecone's service, and FAISS, Cedar and Casbin in process. Branch coverage must
  stay at 90% or more for the package and 95% or more for the code that decides access (the retriever, filters, authorizers and cache).
- **Fuzzing.** ClusterFuzzLite (Atheris) fuzzes filter parsing and the translations that write query
  text, on pull requests and weekly.
- **Static analysis.** CodeQL on every change; ruff with the bandit rules; mypy `--strict`.
- **Supply chain.** Every CI and release install is pinned by hash in `uv.lock`; releases are built
  in GitHub Actions, published with trusted publishing, signed with Sigstore, and carry a CycloneDX
  SBOM, with SLSA provenance from 1.4.0. After each release a workflow installs it from PyPI, pinned to
  the hash of the file on the GitHub release, and runs it. OpenSSF Scorecard runs on every change, and
  a check fails when one of its scores drops.
- **Dependencies.** The core has none at run time. The optional extras and the test tools are listed
  with their hashes in `uv.lock`, and each release's SBOM lists what it was built with. Advisories
  that have no fix yet are listed with reasons in `osv-scanner.toml`.
- **Benchmarks.** [Benchmarks](benchmarks.md): recall, latency and leak checks at up to a million
  chunks, with the scripts and settings to reproduce them.

## Reproducing it

- Without external services: `pip install -e ".[dev]"`, then `pytest`. The suites that need a store
  or a permission service skip themselves.
- With them: the `test` job in
  [`.github/workflows/ci.yml`](https://github.com/muzakkirhussain011/wardowl/blob/main/.github/workflows/ci.yml)
  starts each server (images and versions are listed there) and sets the `WARDOWL_TEST_*`
  variables that switch each suite on. Run the same containers and set the same variables.
- For your own backend or authorizer: the [conformance kit](extending.md#test-it-with-the-conformance-kit).

## Findings so far

Found by the project's own review and tests, all fixed with regression tests. The first six are
published as advisory
[GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2).

| Found | Problem | Fixed in |
|---|---|---|
| October 2026 | `invalidate()` could be undone by a retrieval in flight, for up to `ttl`, in every replica sharing the cache | 1.2.1, 1.3.1 |
| October 2026 | With a shared cache, a retrieval running during `invalidate()` could copy the old entry back into the invalidating process, for up to `local_ttl` | 1.2.1, 1.3.2 |
| October 2026 | `invalidate(subject)` cleared nothing unless given a subject equal in every field | 1.2.1, 1.3.1 |
| October 2026 | An `invalidate()` of everyone deleted the epoch that a concurrent `invalidate()` of one id had just written, so a retrieval in flight could store its earlier answer for up to `ttl` | 1.2.2, 1.3.3 |
| October 2026 | In the invalidating process, a read of the global epoch already in flight could put back the epoch it replaced, for up to `local_ttl` | 1.2.2, 1.3.3 |
| October 2026 | An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not `local_ttl` | 1.2.2, 1.3.3 |
| October 2026 | Epochs stored as raw bytes broke shared caches whose Redis client decodes responses: every lookup went to the permission service | 1.2.2, 1.3.3 |
| October 2026 | A retrieval event that could not be built replaced the retrieval's result or error | 1.3.3 |
| October 2026 | A ListObjects answer cut short by OpenFGA's deadline was used as a complete pre-filter (results hidden, reported complete) | 1.4.0 |
| October 2026 | With `recheck=False`, a backend that ignored the filter could leak | 1.4.0 |
| October 2026 | A subject attribute named `groups` replaced the subject's groups in Cerbos decisions | 1.4.0 |
| October 2026 | A cancelled retrieval produced no audit event | 1.4.0 |
| October 2026 | The example gateway stored API keys as unsalted SHA-256 | the gateway example |
| October 2026 | Two caching authorizers sharing one Redis under the default namespace answered for each other, so `AllOf` could skip one of them | 2.0.0 |
| October 2026 | Cache keys joined their parts without lengths and compared values loosely, so `1` and `"1"`, or a list and a tuple, could share a cached decision | 2.0.0 |
| October 2026 | A shared-cache entry with a NaN expiry gave a local copy that never expired | 2.0.0 |
| October 2026 | `SpanRedactor` showed a chunk in full when its spans field was missing, which `set_metadata` without the field caused | 2.0.0 |
| October 2026 | Haystack's retriever took the subject as a run input, which an agent's model could set | 2.0.0 |
| October 2026 | The framework retrievers passed every metadata key, permission fields included, to the model, prompts and traces | 2.0.0 |
| October 2026 | A subject's attributes could be changed after the subject was used as a cache key | 2.0.0 |
| October 2026 | FAISS kept earlier copies of an id repeated in one `add`, out of reach of `delete` and `set_metadata` | 2.0.0 |
| October 2026 | Qdrant and Weaviate kept a UUID chunk id as the point id, so another spelling of it, or a crafted id, landed on the same point | 2.0.0 |
| October 2026 | Milvus `delete` and `set_metadata` wrote ids into the expression unescaped, so one id could select others | 2.0.0 |
| October 2026 | `set_metadata` wrote whole chunks back, so a chunk deleted meanwhile could return with its old text | 2.0.0 |
| October 2026 | Cassandra's `set_metadata` used a lightweight transaction, whose timestamp could put an older permission over a newer one | 2.0.0 |
| October 2026 | pgvector matched `In("level", [1])` against a stored `1.0` | 2.0.0 |
| October 2026 | The public filter translators returned `None` or `{}` for a filter that matches nothing, which store clients read as no filter | 2.0.0 |
| October 2026 | Vespa and Redis returned partial results (degraded coverage, a Query Engine timeout) as complete | 2.0.0 |
| October 2026 | Cedar dropped float, None and oversized integer attributes, so a `forbid` on them stopped applying | 2.0.0 |
| October 2026 | Casbin sent groups as bare subjects, so a group named like a user or role got its access | 2.0.0 |
| October 2026 | Google Drive grants with an expiry were kept without it, and a deleted file kept its grants | 2.0.0 |
| October 2026 | The HTTP authorizers could share a kept-alive connection with a forked worker, and their timeout bounded each socket operation, not the call | 2.0.0 |
| October 2026 | OpenFGA accepted subject ids holding `:`, `#` or `*`, which change the meaning of its `type:id` strings | 2.0.0 |
| October 2026 | The HTTP service ran a synchronous authenticator on the event loop, and the OIDC key cache never expired and could be refetched on every request | 2.0.0 |
| October 2026 | The OIDC authenticator verified with the algorithm the token named, and malformed tokens gave 500 errors | 2.0.0 |
| October 2026 | The HTTP service parsed bodies of any size and passed NaN vectors to the retriever | 2.0.0 |
| October 2026 | The Lock workflow resolved dependencies with a token that could push | 2.0.0 |
| October 2026 | Haystack's `ComponentTool` turned a subject a model sent, even one it did not offer, into a real `Subject` | 2.0.0 |
| October 2026 | LlamaIndex's node postprocessor passed permission fields into prompts and traces | 2.0.0 |
| October 2026 | pgvector stored a whole-number float written with an exponent as an integer, which an integer filter matched | 2.0.0 |
| October 2026 | FAISS returned its stored metadata, so changing a hit changed the chunk's permissions | 2.0.0 |
| October 2026 | Elasticsearch and OpenSearch returned results from a search with failed shards as complete | 2.0.0 |
| October 2026 | Cedar never received metadata keys starting with `__`, so a `forbid` on them did not apply | 2.0.0 |
| October 2026 | One chunk id OpenFGA or SpiceDB refused failed every retrieval that returned it | 2.0.0 |
| October 2026 | A string `audit_key` or `signing_key` turned audit events or the shared cache off without an error | 2.0.0 |
| October 2026 | The example gateway parsed bodies of any size before authentication, and two of its patterns took quadratic time | the gateway example |
| October 2026 | With SpiceDB's `group_subjects=True`, the default, a banned user read a document shared with their group under an exclusion such as `viewer - banned` | 2.1.0 |
| October 2026 | A permission service's error text, host names and credentials included, reached models through agent tools | 2.1.0 |
| October 2026 | KetoAuthorizer's default batch of 100 exceeded Keto's default limit of 10, so retrievals of more than 10 chunks failed | 2.1.0 |
| October 2026 | A chunk shortened after its redaction spans were written made every retrieval that reached it fail | 2.1.0 |
| October 2026 | Chroma refused one large write, so loading a big corpus failed | 2.1.0 |

## Reporting

Report anything you find privately, as [SECURITY.md](https://github.com/muzakkirhussain011/wardowl/blob/main/SECURITY.md)
describes.
