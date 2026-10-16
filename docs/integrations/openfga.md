# OpenFGA

Relationship-based access control in the style of Google Zanzibar. No extra dependency: requests
use the standard library.

```python
from needtoknow.authorizers.openfga import OpenFGAAuthorizer

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
resource. A chunk without its resource id is denied.

Change the names with `object_type=`, `user_type=`, `relations={"read": "viewer"}`,
`group_type=` and `group_relation=`. Pass `authorization_model_id=` to pin a model version.

## Checks

`check` sends BatchCheck requests, `batch_size` (default 50) resources at a time, and asks about each
distinct resource once even when several chunks share it. A per-item error, or a response that does
not decide every resource, raises `AuthorizationError`.

## Pre-filtering with ListObjects

`filter_for` calls ListObjects for every document the user may view and returns
`In(resource_field, ids)`, so the vector store pre-filters. OpenFGA caps how many objects ListObjects
returns, so when the answer reaches `list_objects_limit` (default 1000) it may be incomplete. The
authorizer then returns no filter, and the retriever post-filters with BatchCheck instead of trusting
a truncated list. Set `list_objects_limit` to your server's limit.

## Groups from your identity provider

With `contextual_groups=True` (the default), the subject's groups are sent with every request as
contextual tuples (`user:<id> member group:<name>`), so group memberships from your identity
provider take effect without being written to OpenFGA first. They are trusted as given, so they
must come from authentication, not from the client.

## Failing closed

Every failure raises `AuthorizationError`: an unreachable server, a timeout (`timeout=`, 2 seconds
by default), an unexpected response, or an action with no configured relation. `headers=` adds
headers to every request, for example an API token.

For high traffic, wrap it in a [`CachingAuthorizer`](../operations.md#permission-caching).
