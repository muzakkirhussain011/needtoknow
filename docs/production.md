# Production checklist

What to settle before needtoknow serves real users, and what happens when each part of the system
fails. Each item links to the page with the details.

## Before launch

**Identity**

- [ ] The `Subject` is built from your authenticated session: the user id and groups your identity
  provider vouches for, never values from the request body, and never a tool argument a language
  model fills in. ([Security model](security.md#the-subject-must-come-from-authentication))
- [ ] Only groups that grant access to documents are on the subject. A subject in tens of thousands
  of groups works, but costs on every query. ([Operations](operations.md#subjects-in-very-many-groups))
- [ ] In a multi-tenant deployment, every subject carries its tenant, and the authorizer is wrapped
  in `AllOf(TenantAuthorizer(), ...)`. A subject without a tenant then gets nothing.
  ([Combining authorizers](combining.md))

**Permissions on the chunks**

- [ ] Permission metadata is copied onto every chunk at ingestion, and re-copied when a document's
  sharing changes. How soon that happens is your revocation delay for those chunks.
  ([Security model](security.md#what-it-relies-on))
- [ ] The permission fields are indexed in your vector store: `create_schema` (pgvector),
  `create_collection` (Qdrant, Milvus) and `create_index` (Elasticsearch, OpenSearch) create the
  indexes; Chroma needs no setup. ([Integrations](integrations/index.md))
- [ ] Your own backend or authorizer, if you wrote one, passes the conformance kit.
  ([Extending needtoknow](extending.md#test-it-with-the-conformance-kit))

**Retrieval settings**

- [ ] `recheck=True` (the default) stays on. It also catches a filter that is broader than the
  authorizer's own check; with `recheck=False` only a backend that ignores or mistranslates the
  filter is caught.
- [ ] `strategy="auto"` (the default) or `"prefilter"` when the authorizer can return a filter.
  Post-filtering alone loses recall for users who may read little of the index.
  ([Benchmarks](benchmarks.md#what-the-load-results-show))
- [ ] `max_candidates` is sized for your least-privileged users, and `complete=False` is monitored.
- [ ] A permission change at the source reaches the chunks' metadata (`set_metadata`) within your
  revocation target, and you know each source document's chunk ids.
  ([Operations](operations.md#changing-a-documents-permissions))

**The permission service**

- [ ] HTTP authorizers have a `timeout` and `retries` you have chosen, and a shared
  `CircuitBreaker` so a failing service is not flooded.
  ([Operations](operations.md#resilience-of-remote-permission-services))
- [ ] Connections use TLS and credentials (`headers=`, or `token=` for SpiceDB).
- [ ] `CachingAuthorizer` sits in front, with a `ttl` chosen as a security decision: it is the
  longest a revoked permission keeps granting access. ([Operations](operations.md#permission-caching))
- [ ] With several replicas, the shared cache has a `signing_key` and a Redis reachable only by the
  application. ([Security model](security.md#what-it-relies-on))

**Observability**

- [ ] Audit events go to durable storage (`JsonLinesAudit` or your own observer), with an
  `audit_key` from your secret store if you want query fingerprints. ([Operations](operations.md#audit-events))
- [ ] `OpenTelemetryMetrics` and `OpenTelemetryObserver` are installed, and the alerts in
  [What to alert on](operations.md#what-to-alert-on) are set up, starting with any increase in
  `needtoknow.violations`.
- [ ] `examined`, `denied` and timings stay in logs and metrics and are never shown to end users;
  they can hint that forbidden documents exist. ([Security model](security.md#what-it-does-not-do))

**Supply chain and capacity**

- [ ] The version is pinned, and the release's signatures are verified as
  [SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md#verifying-a-release)
  describes.
- [ ] Capacity is measured on your own data with the load harness in `benchmarks/`
  ([Benchmarks](benchmarks.md#reproduce-it)). In ours, over a million chunks, one 4-CPU machine
  shared by PostgreSQL and the load generator peaked at 129 requests a second.

## When something fails

needtoknow fails closed: whenever it cannot be sure what a subject may read, it returns nothing and
says why. The table shows what each failure looks like.

| What fails | What needtoknow does | What you see |
|---|---|---|
| The permission service is down, slow or erroring | Raises `AuthorizationError` after the timeout and retries; with a circuit breaker open, at once | `needtoknow.retrievals` with `error.type = AuthorizationError`; no results reach the model |
| The permission service gives a malformed answer (wrong count, non-booleans, not a filter) | Raises `AuthorizationError`; never caches the answer | As above |
| The vector store is down | Raises the store client's own error; nothing is returned | `error.type` names that error |
| The vector store ignores or mistranslates the filter | The re-check drops every chunk outside it | `violations > 0` on the result, the event and the `needtoknow.violations` metric |
| Post-filtering cannot find `k` readable chunks within `max_candidates` | Returns what it found | `complete = False`; `needtoknow.outcome = incomplete` |
| A subject has no tenant under `TenantAuthorizer` | Raises `AuthorizationError` | As for a failing authorizer |
| The shared cache (Redis) is down or slow | Treats it as a miss and asks the permission service | A warning on the `needtoknow` logger; higher `needtoknow.authorize.duration` |
| A shared-cache entry is unsigned, tampered with or copied under another key | Ignores it (with `signing_key`) | Nothing: it is a miss |
| `invalidate()` cannot reach Redis | Raises, because the caller asked for a revocation now | The exception at the caller |
| An observer raises | Logs it; retrieval is unaffected | An error on the `needtoknow` logger |

What needtoknow does not protect against is listed in the
[security model](security.md#what-it-does-not-do): output control, prompt injection inside readable
chunks, and the side channels above.
