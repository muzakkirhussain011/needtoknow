# OpenFGA

Relationship-based access control in the style of Google Zanzibar. No extra dependency: requests
use the standard library.

```python
from wardowl.authorizers.openfga import OpenFGAAuthorizer

authorizer = OpenFGAAuthorizer("http://localhost:8080", store_id, resource_field="doc_id")
```

## The model

The authorizer asks whether `user:<subject id>` has a relation (by default `viewer` for the action
`read`) on `document:<resource id>`. A model such as this one fits:

```text
model
  schema 1.1

type user

type group
  relations
    define member: [user]

type document
  relations
    define viewer: [user, user:*, group#member]
```

Chunks of one source document share its id in metadata (`doc_id` above), so a permission is
written once per document, not once per chunk. With `resource_field=None` the chunk id is the
resource. A chunk without its resource id, or whose field holds anything but one string (a list,
a number), is denied.

Subject ids and group names are written into OpenFGA's `type:id` strings, where `bob#manager`
would be a userset, `*` the wildcard and a `:` another type. An id or group name that is empty or
contains `:`, `#`, `*` or whitespace therefore raises `AuthorizationError` instead of being sent.

Change the names with `object_type=`, `user_type=`, `relations={"read": "viewer"}`,
`group_type=` and `group_relation=`. Pass `authorization_model_id=` to pin a model version.

## Checks

`check` sends BatchCheck requests, `batch_size` (default 50) resources at a time, and asks about each
distinct resource once even when several chunks share it. A per-item error, or a response that does
not decide every resource, raises `AuthorizationError`.

## Pre-filtering with ListObjects

`filter_for` calls ListObjects for every document the user may view and returns
`In(resource_field, ids)`, so the vector store pre-filters.

OpenFGA stops a ListObjects call when it has found `OPENFGA_LIST_OBJECTS_MAX_RESULTS` objects (1000
by default) or when it reaches `OPENFGA_LIST_OBJECTS_DEADLINE` (3 seconds by default), and answers
with what it found so far, without marking the answer as partial. A partial list would never let
anyone read more than they may, but it would hide documents they may read while the result still
looked complete. So when the answer reaches `list_objects_limit` (default 1000), or the call took
`list_objects_deadline` seconds or more (default 3), the authorizer returns no filter and the
retriever post-filters with BatchCheck instead. Set both to your server's values;
`list_objects_deadline=None` suits a server configured without a deadline. With the default
`timeout` of 2 seconds, a call slow enough to meet the default deadline fails first and raises.

`In` matches a list field when any of its elements is listed, while `check` denies a chunk whose
resource field is not one string. The retriever's re-check of pre-filtered hits (`recheck=True`,
the default) drops such a chunk and counts it in `violations`. With `recheck=False` the filter
alone decides, so the resource field must be one string on every chunk.

## Consistency

`consistency="HIGHER_CONSISTENCY"` asks OpenFGA to skip its cache for every check and list, so a
tuple written a moment ago counts at once, at a cost in latency. `"MINIMIZE_LATENCY"`, OpenFGA's
default, may answer from its cache when one is configured (caching is off by default), so a revoked
tuple can keep granting access until that cache entry expires. Leaving `consistency` unset sends
nothing and lets the server decide. Choose it together with a
[`CachingAuthorizer`](../operations.md#permission-caching)'s `ttl`: an answer the cache keeps may
itself have come from OpenFGA's cache, so the revocation delay can reach the two added together.

## Groups from your identity provider

With `contextual_groups=True` (the default), the subject's groups are sent with every request as
contextual tuples (`user:<id> member group:<name>`), so group memberships from your identity
provider take effect without being written to OpenFGA first. They are trusted as given, so they
must come from authentication, not from the client.

## Failing closed

Every failure raises `AuthorizationError`: an unreachable server, a timeout (`timeout=`, 2 seconds
by default, for the whole call), an unexpected response or one larger than `max_response_bytes=`
(10 MiB by default), or an action with no configured relation. `headers=` adds headers to every
request, for example an API token; an `Authorization` header over `http://` to another machine
needs `allow_insecure=True`, as for [OPA](opa.md#failing-closed). Connections are reused and
transient failures retried once (`retries=`); `circuit_breaker=` and `ssl_context=` work as
described under [resilience](../operations.md#resilience-of-remote-permission-services).

For high traffic, wrap it in a [`CachingAuthorizer`](../operations.md#permission-caching).
