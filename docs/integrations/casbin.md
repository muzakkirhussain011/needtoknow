# Casbin

[Casbin](https://casbin.org) evaluates access-control models (ACL, RBAC, ABAC) written in its own
model language, with policies stored wherever its adapters reach: a file, a database, a policy
service. `CasbinAuthorizer` asks the `casbin.Enforcer` you already use about each chunk, in your
process.

```bash
pip install "wardowl[casbin]"
```

```python
import casbin

from wardowl import PermissionedRetriever
from wardowl.authorizers.casbin import CasbinAuthorizer

enforcer = casbin.Enforcer("rbac_model.conf", adapter)  # your model and policy adapter
retriever = PermissionedRetriever(backend, CasbinAuthorizer(enforcer))
```

## The request

Each chunk is asked about as `(subject.id, object, action)`: the object is `metadata["doc_id"]`
(`resource_field`; the chunk id when it is None) and the action `read` (map others with
`actions={"read": "view"}`). A chunk without its object is denied. Decisions go through
`batch_enforce`, one call per retrieval.

For ABAC models whose matchers read attributes, `request` builds the request yourself:

```python
from types import SimpleNamespace


def request(subject, hit, action):
    return (SimpleNamespace(id=subject.id, **subject.attributes), SimpleNamespace(**hit.metadata), action)


authorizer = CasbinAuthorizer(enforcer, request=request)
```

## Groups from the token

If group memberships live in Casbin's `g` policies, nothing else is needed. If they come from your
identity provider, `group_subjects=True` also asks with each of the subject's groups in place of its
id, and allows a chunk if any answer does. Use it only with allow-only models: with deny effects,
a deny for one group would not override an allow for another, so keep memberships in `g` instead.

## Pre-filtering and failures

Casbin answers yes or no per request and has no reverse lookup, so `filter_for` returns None and
the retriever post-filters, deciding every candidate. An enforcer that raises, or answers with
anything but one boolean per request, makes the retrieval raise `AuthorizationError`. To share one
enforcer between threads while policies change, use `casbin.SyncedEnforcer`.
