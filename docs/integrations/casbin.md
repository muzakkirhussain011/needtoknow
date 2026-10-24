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

With `request`, wardowl sends exactly the request the function returns, one per chunk, and nothing
more: `group_subjects`, `group_prefix`, the refusal of user ids that start with the prefix, and
`resource_field` are all skipped, and the function receives the Casbin action (after `actions`). If
your policies name groups in the subject field (`p, group:legal, contract-7, read`), a user whose id
is `group:legal` would then get that group's grants. A custom `request` must therefore:

- refuse an id that could pass for a group or a role, by raising `AuthorizationError` (anything it
  raises makes the retrieval raise `AuthorizationError`);
- carry the subject's groups itself when they come from the token, for example as an attribute the
  matcher reads, since `group_subjects` does not apply;
- describe a chunk that lacks the attributes the matcher reads in a way the policy denies.

```python
from wardowl import AuthorizationError


def request(subject, hit, action):
    if subject.id.startswith("group:"):
        raise AuthorizationError(f"subject id {subject.id!r} could pass for a group")
    who = SimpleNamespace(id=subject.id, groups=sorted(subject.groups), **subject.attributes)
    return (who, SimpleNamespace(**hit.metadata), action)
```

## Groups from the token

If group memberships live in Casbin's `g` policies, nothing else is needed. If they come from your
identity provider, `group_subjects=True` also asks with each of the subject's groups in place of its
id, and allows a chunk if any answer does. Use it only with allow-only models: with deny effects,
a deny for one group would not override an allow for another, so keep memberships in `g` instead.

Users, roles and these groups all arrive in Casbin's one subject field, so each group is sent with
`group_prefix` in front (`"group:"` by default): the group `eng` is asked about as `group:eng`, and
policies that grant to groups must name them that way.

```text
p, group:eng, design-doc, read
p, alice, design-doc, read
```

Without the prefix, a group named like a user or a role (`admin`, `ceo@example.com`) would be given
that user's or role's access. A subject whose id starts with the prefix raises
`AuthorizationError`, since it could pass for a group; pick a prefix your user ids never start with
(`group_prefix="team/"`, say). An empty prefix is refused.

## Pre-filtering and failures

Casbin answers yes or no per request and has no reverse lookup, so `filter_for` returns None and
the retriever post-filters, deciding every candidate. An enforcer that raises, or answers with
anything but one boolean per request, makes the retrieval raise `AuthorizationError`. To share one
enforcer between threads while policies change, use `casbin.SyncedEnforcer`.
