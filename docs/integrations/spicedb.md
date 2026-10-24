# SpiceDB

Relationship-based access control in the style of Google Zanzibar, through SpiceDB's HTTP API. No
extra dependency: requests use the standard library. Start SpiceDB with its HTTP gateway:

```bash
spicedb serve --grpc-preshared-key "$SPICEDB_KEY" --http-enabled   # HTTP on :8443
```

```python
from wardowl.authorizers.spicedb import SpiceDBAuthorizer

authorizer = SpiceDBAuthorizer("http://localhost:8443", token=SPICEDB_KEY, resource_field="doc_id")
```

The token is a bearer token, so it is refused with `ValueError` over `http://` to any host but this
machine (`localhost`, `127.0.0.1`, `::1`, with no proxy in the way): serve SpiceDB's HTTP gateway
with TLS (`--http-tls-cert-path` and `--http-tls-key-path`) and use an `https://` url, or pass
`allow_insecure=True` on a network you trust. `ssl_context=`, for your own CA or mutual TLS, is
refused with `http://`, where it would not be used.

## The schema

The authorizer asks whether `user:<subject id>` has a permission (by default `view` for the action
`read`) on `document:<resource id>`. A schema such as this one fits:

```text
definition user {}

definition group {
    relation member: user
}

definition document {
    relation viewer: user | user:* | group#member
    permission view = viewer
}
```

Chunks of one source document share its id in metadata (`doc_id` above), so a relationship is
written once per document, not once per chunk. With `resource_field=None` the chunk id is the
resource. A chunk without its resource id, or whose field holds anything but one string (a list,
a number), is denied. Change the names with `resource_type=`, `subject_type=`,
`permissions={"read": "view"}`, `group_type=` and `group_relation=`. Subject ids and group names
are sent as separate fields of SpiceDB's JSON, never inside a `type:id#relation` string, so a `:`
or `#` in one cannot change its type or relation.

SpiceDB object ids are 1 to 1024 letters, digits and `/_|-=+` (its API's rule; `*` alone is the
wildcard, which only subjects use). Store such an id in the chunk's `resource_field` and use such
subject ids (for example a stable user id rather than an email address). A chunk whose resource
id breaks the rule is denied without being sent, since SpiceDB would refuse the whole request and
every retrieval that found the chunk would return nothing. A subject id or group name SpiceDB
rejects makes the request fail, so retrieval fails closed.

## Groups from your identity provider

By default the subject's groups are checked as subject sets, `group:<name>#member`, as well as the
user itself. A document shared with `group:eng#member` is then readable by anyone whose `Subject`
lists `eng`, without syncing group memberships into SpiceDB. If memberships do live in SpiceDB,
pass `group_subjects=False` so only the user is checked.

## Checks

`check` sends CheckBulkPermissions requests of `batch_size` questions (100 by default): one per
distinct resource and subject. Answers are matched to questions by the request SpiceDB echoes
with each one, not by position. A conditional answer (a caveat whose context was not supplied) is
not a decision and raises `AuthorizationError`; supply caveat context with
`caveat_context=lambda subject: {...}`.

## Pre-filtering

`filter_for` calls LookupResources for the user and for each group subject, and turns the union of
the documents found into `In("doc_id", ...)`, so the vector store filters inside the search. If a
lookup returns `lookup_limit` documents (1000 by default) the list may be truncated, so no filter
is returned and the retriever post-filters instead; it does the same when a lookup result is
conditional on a caveat. Subjects who may read very many documents are therefore post-filtered,
which stays correct.

`In` matches a list field when any of its elements is listed, while `check` denies a chunk whose
resource field is not one string. The retriever's re-check of pre-filtered hits (`recheck=True`,
the default) drops such a chunk and counts it in `violations`. With `recheck=False` the filter
alone decides, so the resource field must be one string on every chunk.

## Consistency and revocation

The default consistency, `{"minimizeLatency": True}`, lets SpiceDB answer from a recent snapshot.
A revoked permission can then keep granting access for up to the datastore's revision quantization
interval, 5 seconds by default. For fresher answers pass `consistency={"fullyConsistent": True}`,
or `{"atLeastAsFresh": {"token": zedtoken}}` with the ZedToken from your last write. Combined with
`CachingAuthorizer`, the revocation bound is the cache's `ttl` plus this interval.

## Failures

Every failure raises `AuthorizationError`, so retrieval returns nothing: an unreachable server, a
timeout (`timeout=`, 2 seconds by default, for the whole call except the DNS lookup of the host, as
[for OPA](opa.md#failing-closed)), an HTTP error, a malformed answer or one larger than
`max_response_bytes=` (10 MiB by default), a per-item error, or a missing answer. Connections are
reused, transient failures are retried once, and a `CircuitBreaker` can pause requests to a failing
server (see [Operations](../operations.md#resilience-of-remote-permission-services)).

CI checks the authorizer against a real SpiceDB (v1.56.2): for random relationship graphs, every
strategy returns exactly the reference answer, and `check` and `filter_for` pass the
[conformance kit](../extending.md#test-it-with-the-conformance-kit).
