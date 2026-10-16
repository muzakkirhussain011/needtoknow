# Production: results, auditing, monitoring

## What a result tells you

```python
result = permissioned.retrieve(question, subject, k=5)
result.hits  # permitted chunks, best first, at most k
result.strategy  # "prefilter" or "postfilter"
result.complete  # False when results may be missing (post-filtering hit max_candidates)
result.violations  # pre-filtered hits the re-check rejected: should be 0, alert otherwise
```

`PermissionedRetriever(backend, authorizer, strategy="auto", max_candidates=1000, recheck=True)`.
Keep `recheck=True`: it is what makes a store that ignores the filter unable to leak.

## Audit and tracing

```python
import sys

from needtoknow import JsonLinesAudit
from needtoknow.otel import OpenTelemetryObserver

permissioned = PermissionedRetriever(
    backend,
    authorizer,
    observers=[JsonLinesAudit(sys.stdout), OpenTelemetryObserver()],
    audit_key=secret_bytes,  # optional: adds an HMAC of the query, never the query itself
)
```

Every retrieval, including failed ones, emits a `RetrievalEvent`: subject id, strategy, returned ids,
denied and violation counts, duration and error type. Never query or chunk text. A broken observer
is logged and never changes what retrieval returns.

## Remote permission services

```python
from needtoknow import CachingAuthorizer
from needtoknow.authorizers import CircuitBreaker

authorizer = CachingAuthorizer(OpenFGAAuthorizer(url, store_id, circuit_breaker=CircuitBreaker()), ttl=30)
```

Requests reuse connections and retry transient failures once; timeouts and other errors fail
closed. The circuit breaker stops calling a service that keeps failing and probes it later.

## Alert on

- `violations > 0`: the vector store returned chunks outside the filter.
- A rising rate of `AuthorizationError`: the permission service is failing (retrievals return nothing).
- `complete == False` often: subjects who may read little are post-filtered; give the authorizer a
  `filter_for` or raise `max_candidates`.
