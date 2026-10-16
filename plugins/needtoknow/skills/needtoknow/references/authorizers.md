# Authorizers: where the permissions live

An authorizer answers `check(subject, hits, action) -> list[bool]` (one decision per hit) and
`filter_for(subject, action) -> Filter | None` (a filter for pre-filtering, or None). Any failure
raises `AuthorizationError` and the retriever returns nothing.

## Permissions on the chunks

```python
from needtoknow import MetadataACLAuthorizer

MetadataACLAuthorizer(users_field="allowed_users", groups_field="allowed_groups", everyone="*")
```

A chunk is readable when its `allowed_users` contains the subject's id, or its `allowed_groups`
contains one of the subject's groups or `"*"`. A chunk with neither field is readable by nobody.
Best when permissions are copied from the source system (SharePoint, Google Drive, Confluence, a
database) at ingestion time.

## Your own rules

```python
from needtoknow import FunctionAuthorizer, In


def check(subject, hits, action):
    return [hit.metadata.get("tenant") == subject.attributes["tenant"] for hit in hits]


def filter_for(subject, action):
    return In("tenant", [subject.attributes["tenant"]])  # optional: enables pre-filtering


authorizer = FunctionAuthorizer(check, filter_for=filter_for)
```

`check` must return real booleans; anything else is rejected. Filters: `In(field, values)`,
`And(...)`, `Or(...)`; `needtoknow.matches(filter, metadata)` is the reference semantics.

## Policy and relationship services

All three use the standard library's HTTP client (no extra), reuse connections, retry transient
failures once, and accept `circuit_breaker=CircuitBreaker()` and `ssl_context=`.

```python
from needtoknow.authorizers.opa import OPAAuthorizer
from needtoknow.authorizers.openfga import OpenFGAAuthorizer
from needtoknow.authorizers.spicedb import SpiceDBAuthorizer

OPAAuthorizer("http://opa:8181", decisions_path="needtoknow/decisions", filter_path="needtoknow/filter")
OpenFGAAuthorizer("http://openfga:8080", store_id, resource_field="doc_id")
SpiceDBAuthorizer("http://spicedb:8443", token=key, resource_field="doc_id")
```

- OPA: the policy returns one decision per chunk at `decisions_path`, and optionally a filter at
  `filter_path`. A complete example policy is `examples/opa/needtoknow.rego` in the repository.
- OpenFGA and SpiceDB decide per document: put the document's id in each chunk's `doc_id`. The
  subject's groups are honoured without syncing them (contextual tuples in OpenFGA, `group:<g>#member`
  subject sets in SpiceDB).

## Caching

```python
from needtoknow import CachingAuthorizer

CachingAuthorizer(authorizer, ttl=30.0, maxsize=100_000)
```

Caches filters and per-chunk decisions per subject. `ttl` is the revocation bound: a revoked
permission can grant access for up to `ttl` seconds (plus the service's own lag). Errors and
malformed answers are never cached. `invalidate(subject)` drops a subject's entries.

## Check an authorizer

```python
from needtoknow.testing import check_authorizer

check_authorizer(authorizer, subjects, hits)  # check() and filter_for() must agree
```
