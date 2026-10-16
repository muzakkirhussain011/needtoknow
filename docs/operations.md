# Operations

Running needtoknow in production: fewer calls to the permission service, an audit trail, traces,
and the signals worth alerting on.

```python
from needtoknow import CachingAuthorizer, JsonLinesAudit, PermissionedRetriever
from needtoknow.otel import OpenTelemetryObserver  # pip install "needtoknow[otel]"

retriever = PermissionedRetriever(
    backend,
    CachingAuthorizer(authorizer, ttl=30),
    observers=[JsonLinesAudit(audit_file), OpenTelemetryObserver()],
    audit_key=audit_key,  # bytes from your secret store; enables query fingerprints
)
```

## Permission caching

A remote permission service (OPA, OpenFGA) adds a network round trip to every retrieval.
`CachingAuthorizer` wraps any authorizer and remembers its answers.

```python
from needtoknow import CachingAuthorizer

authorizer = CachingAuthorizer(openfga, ttl=30, maxsize=100_000)
```

- **What is cached:** per subject and action, the pre-filter from `filter_for` and each `check`
  decision. A decision is keyed by the chunk id *and* its metadata, so a chunk whose permission
  metadata changed is decided again.
- **The revocation bound:** nothing older than `ttl` seconds is used. A permission revoked at the
  source stops granting access within `ttl` seconds, plus whatever delay the permission service
  itself has. Pick `ttl` as a security decision: 30 seconds is a common trade-off, and a few
  seconds still removes most repeated calls within one conversation.
- **Never cached:** exceptions and malformed answers (non-booleans, the wrong number of
  decisions). Every failure reaches the retriever, which fails closed.
- **Size:** at most `maxsize` entries; the least recently used go first.
- **Invalidation:** `invalidate(subject)` drops one subject's entries, for example after a role
  change you handled yourself; `invalidate()` drops everything.

It works for `retrieve` and `aretrieve`, using the wrapped authorizer's async methods when it has
them.

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

from needtoknow.authorizers import CircuitBreaker
from needtoknow.authorizers.openfga import OpenFGAAuthorizer

tls = ssl.create_default_context(cafile="/etc/ssl/internal-ca.pem")
tls.load_cert_chain("/etc/needtoknow/client.pem", "/etc/needtoknow/client.key")

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
| `error` | The exception type when the call failed, for example `AuthorizationError` |
| `query_hmac` | HMAC-SHA256 of the query, only when `audit_key` is set |

Events never contain query or chunk text. A plain hash of a short query could be reversed by
guessing, so the fingerprint is an HMAC under your key, and there is none without a key. Equal
queries under the same key have equal fingerprints, which lets you spot repeated or probing queries
without storing them. Rotating the key breaks that link, which can be what you want.

`JsonLinesAudit(stream)` writes one JSON object per line to any text stream, thread-safely, and
flushes after each event:

```json
{"action":"read","complete":true,"denied":0,"duration_ms":4.21,"error":null,"examined":3,"k":5,"query_hmac":"9f2c...","returned_ids":["handbook","policy-7"],"started_at":1792152000.12,"strategy":"prefilter","subject_id":"bob","violations":0}
```

An observer that raises is logged on the `needtoknow` logger and never changes what retrieval
returns or raises.

## Tracing

`OpenTelemetryObserver` records each retrieval as a span named `needtoknow.retrieve`, timed from
the call and parented to the span that was current when it ran.

| Attribute | Value |
|---|---|
| `needtoknow.action`, `needtoknow.k` | As requested |
| `needtoknow.strategy` | The strategy that ran |
| `needtoknow.returned`, `needtoknow.examined`, `needtoknow.denied`, `needtoknow.violations` | Counts |
| `needtoknow.complete` | As `RetrievalResult.complete` |
| `needtoknow.error` | The exception type; the span status is then ERROR |
| `needtoknow.subject.id` | Only with `OpenTelemetryObserver(record_subject=True)` |

The attributes use the `needtoknow.` prefix because OpenTelemetry's semantic conventions for
generative AI were still in development when this was written. It needs only `opentelemetry-api`;
your application configures the SDK and exporter as usual.

## What to alert on

| Signal | Why |
|---|---|
| `violations > 0` | The backend returned chunks outside the filter. Nothing leaked, because the re-check caught them, but the backend or its translation has a bug. |
| Rate of `AuthorizationError` | The permission service is failing. Users are getting errors, which is the safe outcome, but it is an outage. |
| Share of `complete=False` | Post-filtering is hitting `max_candidates`. Raise it, or move to pre-filtering. |
| `denied` per call, by subject | Unusually high values can mean a user is probing for documents they may not read. |
