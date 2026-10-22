# For security reviewers

Where to start when reviewing needtoknow: what to read, which code decides what, the contracts it
relies on, the APIs worth a second look, the evidence, and how to reproduce it. **No independent
security audit has been carried out yet.** Everything below is the project's own work, which a
review should check rather than take on trust.

## Scope

The `needtoknow` package: the retriever, the filter language, the authorizers, the caching layer,
the store backends and the framework adapters. The example gateway in `examples/gateway` is a
separate deployment with its
[own threat model](https://github.com/muzakkirhussain011/needtoknow/blob/main/examples/gateway/threat-model.md).

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
| `needtoknow/retriever.py` | Which strategy runs, the pre-filter and its re-check, post-filtering and how far it widens, reranking, the validation of every answer, the events | Every result passes through it. One plan serves `retrieve` and `aretrieve`. |
| `needtoknow/filters.py` | The filter language and `matches`, its reference semantics | Every store translation must agree with `matches`; parsing is depth-limited. |
| `needtoknow/authz.py` | `MetadataACLAuthorizer`, `TenantAuthorizer`, `AllOf`, `AnyOf` | Composition, and when a combined filter or scope exists. |
| `needtoknow/caching.py` | Cached decisions, the shared cache, signing, epochs, `invalidate()` | Revocation and replay. |
| `needtoknow/authorizers/` | Requests to OPA, OpenFGA, SpiceDB and Cerbos, and the parsing of their answers | Partial, malformed and truncated answers. |
| `needtoknow/backends/` | Each store's translation of a filter into its query | Injection, type confusion, a filter made broader. |
| `needtoknow/mcp.py`, `langchain.py`, `llamaindex.py`, `haystack.py` | How each framework binds the subject | Impersonation by a client or a model. |

## The contracts

- **A backend** returns hits best first, applies a filter exactly as `matches` defines it, returns
  "nothing can match" as no results (never as no filter), and returns each chunk's metadata as
  stored. `needtoknow.testing.check_backend` checks all four, and every built-in backend passes it
  against its real store in CI.
- **An authorizer**'s `check` returns one real `bool` per hit, in order; `filter_for` returns a filter
  that selects exactly what `check` allows, or None; `scope_for`, when present, returns a condition
  every readable chunk meets. `needtoknow.testing.check_authorizer` checks the first two agree. Any
  failure raises, and the retriever then returns nothing.
- **The subject** comes from your authentication. needtoknow cannot verify it.

## APIs that deserve a second look

- `PermissionedRetriever(recheck=False)`: pre-filtered hits are matched against the filter locally
  but not decided by the authorizer again, so a filter broader than the authorizer's own check goes
  unnoticed.
- `CachingAuthorizer(shared=...)` with the default `namespace`: two differently configured
  authorizers sharing one Redis and namespace share answers.
- `RedisCache` without `signing_key`: whoever can write to that Redis can plant decisions.
- `FunctionAuthorizer` with a `filter_for` broader than its `check`: the re-check catches it (as
  violations); `recheck=False` does not.
- `OpenFGAAuthorizer(contextual_groups=True)` and `SpiceDBAuthorizer(group_subjects=True)`: the
  subject's groups are trusted as given.
- `SpiceDBAuthorizer`'s default consistency, `minimizeLatency`, and OpenFGA's default,
  `MINIMIZE_LATENCY`: answers can trail a write.
- `Subject.attributes`: part of every cache key, and the tenant's source for `TenantAuthorizer`; it
  must come from authentication like the rest of the subject.

## The evidence

- **Tests.** Over 500, many of them Hypothesis property tests, compared with oracles written without
  the library's filter code. CI runs them on Python 3.11 to 3.14 on Linux, the library tests on
  macOS and Windows too, and the suites against 10 vector stores and 4 permission services running
  as real servers. Branch coverage must stay at 90% or more for the package and 95% or more for the
  code that decides access (the retriever, filters, authorizers and cache).
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
  [`.github/workflows/ci.yml`](https://github.com/muzakkirhussain011/needtoknow/blob/main/.github/workflows/ci.yml)
  starts each server (images and versions are listed there) and sets the `NEEDTOKNOW_TEST_*`
  variables that switch each suite on. Run the same containers and set the same variables.
- For your own backend or authorizer: the [conformance kit](extending.md#test-it-with-the-conformance-kit).

## Findings so far

Found by the project's own review and tests, all fixed with regression tests. The first six are
published as advisory
[GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/needtoknow/security/advisories/GHSA-prfv-2pwr-5rx2).

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
| October 2026 | A ListObjects answer cut short by OpenFGA's deadline was used as a complete pre-filter (results hidden, reported complete) | next release |
| October 2026 | With `recheck=False`, a backend that ignored the filter could leak | next release |
| October 2026 | A subject attribute named `groups` replaced the subject's groups in Cerbos decisions | next release |
| October 2026 | A cancelled retrieval produced no audit event | next release |
| October 2026 | The example gateway stored API keys as unsalted SHA-256 | the gateway example |

## Reporting

Report anything you find privately, as [SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md)
describes.
