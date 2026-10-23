# Operations

Running wardowl in production: fewer calls to the permission service, an audit trail, traces,
metrics, and the signals worth alerting on.

```python
from wardowl import CachingAuthorizer, JsonLinesAudit, PermissionedRetriever
from wardowl.otel import OpenTelemetryMetrics, OpenTelemetryObserver  # pip install "wardowl[otel]"

retriever = PermissionedRetriever(
    backend,
    CachingAuthorizer(authorizer, ttl=30),
    observers=[JsonLinesAudit(audit_file), OpenTelemetryObserver(), OpenTelemetryMetrics()],
    audit_key=audit_key,  # bytes from your secret store; enables query fingerprints
)
```

## Permission caching

A remote permission service (OPA, OpenFGA) adds a network round trip to every retrieval.
`CachingAuthorizer` wraps any authorizer and remembers its answers.

```python
from wardowl import CachingAuthorizer

authorizer = CachingAuthorizer(openfga, ttl=30, maxsize=100_000)
```

- **What is cached:** per subject and action, the pre-filter from `filter_for` and each `check`
  decision. A decision is keyed by the chunk id *and* its metadata, so a chunk whose permission
  metadata changed is decided again. Keys encode every value with its type, so `1`, `"1"`, `True`
  and `1.0` are different subjects' attributes or chunks' metadata. A subject or chunk holding a
  value of another type than strings, numbers, booleans, None, bytes, dates, times, decimals, UUIDs
  and lists, tuples, sets and mappings of them is never cached: the authorizer is asked every time.
- **The revocation bound:** nothing older than `ttl` seconds is used. A permission revoked at the
  source stops granting access within `ttl` seconds, plus whatever delay the permission service
  itself has. Pick `ttl` as a security decision: 30 seconds is a common trade-off, and a few
  seconds still removes most repeated calls within one conversation.
- **Never cached:** exceptions and malformed answers (non-booleans, the wrong number of
  decisions, a filter that is not a well-formed `In`, `And` or `Or`). Every failure reaches the
  retriever, which fails closed.
- **Size:** at most `maxsize` entries; the least recently used go first.
- **Monitoring:** `stats()` returns a `CacheStats` with the answers served from this process (`hits`)
  and from the shared cache (`shared_hits`), those the authorizer was asked for (`misses`), the shared
  cache reads and writes that failed (`shared_failures`), and the answers held now (`entries`). Export
  them as gauges to watch the hit rate.
- **Invalidation:** `invalidate(subject)` drops the entries of every subject with that id, whatever
  groups and attributes they came with (pass the `Subject` or just its id), for example after a
  role change you handled yourself; `invalidate()` drops everything. An answer the permission
  service gave before the call, to a retrieval still in progress, is not cached after it: once
  `invalidate()` returns, no retrieval that starts in this process uses an answer from before it.

It works for `retrieve` and `aretrieve`, using the wrapped authorizer's async methods when it has
them.

### Sharing the cache across processes

With many replicas, each one's cache asks the permission service separately, and `invalidate()`
only clears the replica it runs in. Give them a shared cache (`pip install "wardowl[redis]"`):

```python
import redis

from wardowl import CachingAuthorizer
from wardowl.caching import RedisCache

shared = RedisCache(redis.Redis.from_url(REDIS_URL), signing_key=CACHE_SIGNING_KEY)
authorizer = CachingAuthorizer(openfga, ttl=30, shared=shared, namespace="openfga-docs")
```

- **Two tiers:** each process looks in its own cache first, then in Redis, and only then asks the
  permission service; answers go to both. A process keeps its own copy for at most `local_ttl`
  seconds (1 by default), then reads Redis again. Redis, Valkey, Dragonfly and Redis Cluster work
  (the keys for one subject id share a hash slot).
- **The revocation bound still holds.** Each shared entry carries its expiry time, and a replica
  that copies it keeps it only for the time it has left, never a fresh `ttl`. This relies on
  replicas' clocks agreeing, as NTP keeps them.
- **`invalidate(subject)` reaches every replica within `local_ttl`.** It clears its own process and
  Redis at once; every other replica stops using its own copy within `local_ttl` seconds. Lower
  `local_ttl` for faster revocation at the cost of more Redis reads. It raises if Redis cannot be
  cleared, since the caller asked for the revocation now.
- **A late answer from another replica is never read.** A replica may have asked the permission
  service just before the revocation and write its answer to Redis just after. `invalidate()`
  therefore also stores a new random epoch (one per subject id, and one for `invalidate()` of
  everything), and an entry is used only under the epoch it was written in. Epochs stay in Redis
  for `ttl` plus an hour. An attacker who can write to your Redis and copies back a signed entry
  together with its old epoch can extend a decision only until that entry's own expiry, at most
  `ttl` after the permission service gave it.
- **One namespace per policy.** `namespace` is required with a shared cache. Every
  `CachingAuthorizer` with the same Redis and `namespace` uses the same answers, whatever authorizer
  it wraps, so give each differently configured authorizer (another policy, OpenFGA store, tenant
  model or action mapping) its own, and the same one in every replica. Within one process, a second
  `CachingAuthorizer` that wraps an authorizer of another type under a Redis client and namespace
  already in use raises `ValueError`. That check cannot tell two configurations of one type apart,
  nor see other processes, so choosing the names stays your part.
- **Upgrading.** Entries written by an earlier version are not read when the keys or format changed,
  as they did in 1.3 and again with exact, type-aware keys, so the first requests after such an
  upgrade ask the permission service. During a rolling upgrade, an `invalidate(subject)` made by a
  replica of one version does not clear the other version's entries: call `invalidate()` once the
  upgrade is done, or allow for `ttl`.
- **A failing or slow Redis never decides anything.** A read or write that fails is logged and
  treated as a miss: the permission service is asked.
- **Nothing identifying in key names.** Keys are digests of the subject and the chunk, so Redis
  key listings show neither user ids nor document metadata.
- **Sign the values.** Without `signing_key`, anyone who can write to that Redis could plant a
  decision. With it (at least 16 random bytes, for example `secrets.token_bytes(32)`, known only to
  your application), every value carries an HMAC-SHA256 over its key and content, and an entry
  without a valid signature is ignored.

Another store works too: pass any object with `get_many`, `set_many` and `delete_prefix`
(`wardowl.caching.SharedCache`).

## Changing a document's permissions

When a document is shared with someone new, or access to it is taken away, its chunks' metadata
has to change before searches can see it. `set_metadata` replaces the metadata of stored chunks
without embedding them again:

```python
backend.set_metadata({chunk_id: {"allowed_groups": ["hr"]} for chunk_id in chunk_ids})
```

- The whole metadata is replaced: no key of the old permissions survives. Ids that are not stored
  are ignored, so nothing is created from metadata alone.
- It returns once searches see the change, as `add` does for that store
  ([Choosing a store](integrations/index.md#choosing-a-store)); MongoDB waits for its search index.
- Every built-in backend has it, and the [conformance kit](extending.md#test-it-with-the-conformance-kit)
  checks it against each real store. Keep each source document's chunk ids (for example
  `"handbook#12"`) so you can find them when its permissions change.
- With [hybrid search](hybrid.md) over several indexes, update each of them; until they agree,
  `RankFusion` leaves the chunk out.
- To copy sharing from Google Drive, `GoogleDriveACL` reads who may read a file and `with_access`
  writes it into the chunks' metadata ([Google Drive](integrations/google-drive.md)).

A change on the other side, such as a user leaving a group, needs no update to the chunks: the
subject's groups are read at query time. `CachingAuthorizer` keys each decision by the chunk's
metadata, so a chunk whose metadata changed is decided again at once, and cached filters expire
after `ttl`.

## Resilience of remote permission services

`OPAAuthorizer` and `OpenFGAAuthorizer` share an HTTP client built for permission checks on every
retrieval:

- **Connections are reused**, one per thread, so a busy service does not open a TCP and TLS
  connection per check. A connection the server closed while idle is replaced transparently.
- **Transient failures are retried once** (`retries=1`): refused or reset connections, responses
  that never arrived, and HTTP 502, 503 and 504. The requests are queries, so a retry has no side
  effects. Timeouts are not retried, which keeps the worst case at one `timeout`; neither are other
  HTTP errors or invalid answers.
- **A circuit breaker** pauses requests to a service that keeps failing, so retries do not pile on
  during an outage. Retrieval fails closed either way; the breaker makes it fail fast.
- **Redirects are never followed**, and the standard `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`
  variables are honoured (proxied requests do not reuse connections).
- **TLS** uses the system trust store; pass `ssl_context` for your own CA or for mutual TLS.

```python
import ssl

from wardowl.authorizers import CircuitBreaker
from wardowl.authorizers.openfga import OpenFGAAuthorizer

tls = ssl.create_default_context(cafile="/etc/ssl/internal-ca.pem")
tls.load_cert_chain("/etc/wardowl/client.pem", "/etc/wardowl/client.key")

authorizer = OpenFGAAuthorizer(
    "https://openfga.internal:8443",
    store_id,
    timeout=1.0,
    circuit_breaker=CircuitBreaker(failures=5, reset_after=30),
    ssl_context=tls,
)
```

`CircuitBreaker(failures=5, reset_after=30)` opens after five consecutive failed requests (no
response, or a 5xx one), rejects calls at once for 30 seconds, then lets one probe through: success
closes it, failure opens it again. `breaker.is_open` is there for monitoring, and one breaker can be
shared by every authorizer that calls the same service.

## Subjects in very many groups

In large organisations one user can belong to thousands of directory groups. With
`MetadataACLAuthorizer` the pre-filter names all of them, and with OpenFGA or SpiceDB it lists every
document the user may read, so filters can grow large. How each backend sends a large filter, and
what CI tests:

| Backend | A large `In` | Tested in CI |
|---|---|---|
| pgvector | Up to 100 values: one GIN-indexed containment test per value. Above that: two array parameters, so the query stays small and far below PostgreSQL's limit of 65,535 parameters (the GIN index does not serve that clause; the HNSW scan filters as it goes) | a subject in 40,000 groups |
| Qdrant | One `MatchAny` per value type | 10,000 groups |
| Elasticsearch, OpenSearch | One `terms` query, at most `index.max_terms_count` values (65,536 by default) | 10,000 groups |
| Chroma | `$in`, plus one equality clause per value of a list field: large filters slow every query | 300 values |
| OpenFGA, SpiceDB | The pre-filter lists documents, up to `list_objects_limit` / `lookup_limit` (1,000); beyond that the retriever post-filters | |

Every backend also runs the conformance kit with filters of 300 values and subjects in hundreds of
groups. To keep filters small:

- Copy only the groups that grant access to documents into the chunks' permissions, and give the
  `Subject` only those groups; most directory groups (distribution lists, building access) are
  irrelevant to retrieval.
- Use coarser groups where the source allows it: one department rather than its fifty teams.
- Wrap the authorizer in `CachingAuthorizer`, so a large filter is computed once per `ttl`.

## Audit events

Every `retrieve` and `aretrieve` call, including failed ones, produces one `RetrievalEvent` and
passes it to each observer. An observer is any callable taking the event.

| Field | Meaning |
|---|---|
| `subject_id`, `action`, `k` | Who asked, for what, and how many results |
| `strategy` | `prefilter`, `postfilter`, or `None` if the call failed before choosing |
| `returned_ids` | The chunk ids returned, in order |
| `examined`, `denied`, `violations`, `complete` | As in `RetrievalResult` |
| `duration_ms`, `started_at` | Timing (`started_at` is Unix time) |
| `search_ms`, `authorize_ms` | Time spent in the backend and in the authorizer |
| `searches` | Backend searches: one when pre-filtering, more when post-filtering fetched again |
| `reranked`, `rerank_ms` | Chunks given to the [reranker](reranking.md) (0 without one), and the time it took |
| `error` | The exception type when the call failed, for example `AuthorizationError` |
| `cause` | The type of the exception behind `error`: for an `AuthorizationError`, what the call to the permission service raised (`TimeoutError`, `ConnectionRefusedError`), or `None` for a malformed answer |
| `query_hmac` | HMAC-SHA256 of the query, only when `audit_key` is set: of its text, or of its vector when a `Query` has no text |

Events never contain query or chunk text, nor a query's vector. A plain hash of a short query could be reversed by
guessing, so the fingerprint is an HMAC under your key, and there is none without a key. Equal
queries under the same key have equal fingerprints, which lets you spot repeated or probing queries
without storing them. Rotating the key breaks that link, which can be what you want.

`JsonLinesAudit(stream)` writes one JSON object per line to any text stream, thread-safely, and
flushes after each event:

```json
{"action":"read","authorize_ms":0.18,"complete":true,"denied":0,"duration_ms":4.21,"error":null,"examined":2,"k":5,"query_hmac":"9f2c...","rerank_ms":0.0,"reranked":0,"returned_ids":["handbook","policy-7"],"search_ms":3.87,"searches":1,"started_at":1792152000.12,"strategy":"prefilter","subject_id":"bob","violations":0}
```

An observer that raises is logged on the `wardowl` logger and never changes what retrieval
returns or raises.

## Tracing

`OpenTelemetryObserver` records each retrieval as a span named `wardowl.retrieve`, timed from
the call and parented to the span that was current when it ran.

| Attribute | Value |
|---|---|
| `gen_ai.operation.name` | `retrieval` |
| `gen_ai.retrieval.top_k`, `wardowl.k` | The `k` requested |
| `wardowl.action` | As requested |
| `wardowl.strategy` | The strategy that ran |
| `wardowl.returned`, `wardowl.examined`, `wardowl.denied`, `wardowl.violations` | Counts |
| `wardowl.complete` | As `RetrievalResult.complete` |
| `wardowl.searches`, `wardowl.search.duration_ms`, `wardowl.authorize.duration_ms` | Backend searches made, and the time spent in the backend and in the authorizer |
| `wardowl.reranked`, `wardowl.rerank.duration_ms` | Chunks reranked and the reranker's time, on reranked retrievals only |
| `error.type`, `wardowl.error` | The exception type; the span status is then ERROR |
| `wardowl.error.cause` | The type of the exception behind it, as `RetrievalEvent.cause` |
| `wardowl.subject.id` | Only with `OpenTelemetryObserver(record_subject=True)` |

The `gen_ai.*` and `error.type` attributes are the ones OpenTelemetry's GenAI semantic conventions
define for retrieval. Those conventions were still at Development status in October 2026 and may
change, so everything else uses the `wardowl.` prefix, and the span keeps its name. It needs only
`opentelemetry-api`; your application configures the SDK and exporter as usual.

## Metrics

`OpenTelemetryMetrics` turns the same events into metrics for dashboards and alerts:

```python
from wardowl.otel import OpenTelemetryMetrics, OpenTelemetryObserver

observers = [OpenTelemetryObserver(), OpenTelemetryMetrics()]
retriever = PermissionedRetriever(backend, authorizer, observers=observers)
```

| Instrument | Type, unit | What it measures |
|---|---|---|
| `gen_ai.client.operation.duration` | Histogram, s | Each retrieval, with `gen_ai.operation.name = "retrieval"`, as the GenAI conventions define it |
| `wardowl.retrievals` | Counter | Retrievals by `wardowl.outcome`: `complete`, `incomplete` or `error` |
| `wardowl.search.duration` | Histogram, s | Time spent in the backend during one retrieval |
| `wardowl.authorize.duration` | Histogram, s | Time spent in the authorizer during one retrieval |
| `wardowl.rerank.duration` | Histogram, s | Time spent in the reranker, on reranked retrievals only |
| `wardowl.chunks.returned`, `.examined`, `.denied` | Counters | Chunks returned, checked by the authorizer, and withheld |
| `wardowl.violations` | Counter | Pre-filtered chunks the re-check rejected |

Every instrument is broken down by `wardowl.action` and `wardowl.strategy`, plus the outcome
and `error.type` where they apply. Each has a handful of values, so the number of time series stays
small however many users you have. Subjects, queries and chunks are never recorded; per-subject
questions belong to the audit log. The duration histogram uses the bucket boundaries the
conventions advise (10 ms to 82 s); the two stage histograms start at 0.5 ms, because a cached
authorizer answers in well under a millisecond. A `View` in your SDK configuration can change
either.

## What to alert on

| Signal | Metric | Why |
|---|---|---|
| Any violation | `wardowl.violations` increases | The backend returned chunks outside the filter. Nothing leaked, because the re-check caught them, but the backend or its translation has a bug. |
| Authorization failures | `wardowl.retrievals` with `error.type = AuthorizationError` | The permission service is failing. Users are getting errors, which is the safe outcome, but it is an outage. |
| Incomplete results | Share of `wardowl.outcome = incomplete` | Post-filtering is hitting `max_candidates`. Raise it, or move to pre-filtering. |
| Slow permission checks | `wardowl.authorize.duration` p99 | The permission service, or the network to it, is slowing retrieval down. `CachingAuthorizer` helps. |
| Probing | `denied` per call, by subject, in the audit log | Unusually high values can mean a user is probing for documents they may not read. |
| Which part is failing | `error.type = AuthorizationError` by `RetrievalEvent.cause` (or the span's `wardowl.error.cause`) | `TimeoutError` points at a slow permission service, `ConnectionRefusedError` and other `OSError`s at one that cannot be reached, and no cause at an error status (HTTP 503) or an answer in the wrong shape. |
| Shared cache failing | `CachingAuthorizer.stats().shared_failures` rising, or `wardowl shared cache` warnings in the log | Every replica is asking the permission service directly; answers stay correct, but load and latency rise. |
| Cache not helping | Hit rate, `(hits + shared_hits) / (hits + shared_hits + misses)`, falling | `ttl` too short for the traffic, `maxsize` too small, or subjects whose groups or attributes change on every request (each change is a new cache key). |
| Paused permission service | `CircuitBreaker.is_open` | Requests are being refused without calling the service, for `reset_after` seconds. |

When one of these fires, the [incident runbook](runbook.md) has the steps, containment first.
