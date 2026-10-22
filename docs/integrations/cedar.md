# Cedar

[Cedar](https://www.cedarpolicy.com) is a policy language and engine. `CedarAuthorizer` evaluates
Cedar policies in your process with cedarpy, Cedar's Python bindings, so there is no policy service
to run or reach.

```bash
pip install "needtoknow[cedar]"
```

```python
from needtoknow import PermissionedRetriever
from needtoknow.authorizers.cedar import CedarAuthorizer

POLICIES = """
permit (principal, action == Action::"read", resource)
when { resource has allowed_users && resource.allowed_users.contains(principal.id) };

permit (principal, action == Action::"read", resource)
when { resource has allowed_groups && resource.allowed_groups.containsAny(principal.groups) };
"""

retriever = PermissionedRetriever(backend, CedarAuthorizer(POLICIES))
```

## What a policy sees

| Cedar | needtoknow |
|---|---|
| `principal` | `User::"<subject.id>"`, with attributes `id`, `groups` (a set) and the subject's attributes; each group is a parent `Group` entity |
| `action` | `Action::"read"` (map other actions with `actions={"read": "view"}`) |
| `resource` | `Document::"<chunk id>"`, with the chunk's metadata as attributes |

The entity types can be renamed with `principal_type`, `group_type` and `resource_type`, and
`schema` passes a Cedar schema for Cedar to validate against.

Hierarchies come from `entities`, a list of Cedar entities or a function of the subject that returns
them. For example, with payroll inside finance, `principal in Group::"finance"` holds for a member of
payroll:

```python
payroll = {
    "uid": {"__entity": {"type": "Group", "id": "payroll"}},
    "attrs": {},
    "parents": [{"__entity": {"type": "Group", "id": "finance"}}],
}
authorizer = CedarAuthorizer(POLICIES, entities=[payroll])
```

## How it fails

- **No pre-filter.** `filter_for` returns None, so the retriever post-filters: every candidate is
  decided by Cedar, which is exact but searches more for users who may read little. Cedar's partial
  evaluation, which could produce pre-filters, is experimental upstream.
- **Errors deny.** Cedar skips a policy whose evaluation errors, for example on a missing attribute,
  and that includes a `forbid`. needtoknow therefore denies any chunk whose evaluation reported an
  error, and logs how many on the `needtoknow` logger. Guard optional attributes with `has`.
- **Values Cedar cannot hold**, such as floats and None, are left out of a chunk's attributes, so a
  policy reading them errors and the chunk is denied.
- **Configuration** is checked when the authorizer is created: policies Cedar cannot parse raise
  `ValueError`. Any other failure raises `AuthorizationError`, and the retriever returns nothing.
