# Cedar

[Cedar](https://www.cedarpolicy.com) is a policy language and engine. `CedarAuthorizer` evaluates
Cedar policies in your process with cedarpy, Cedar's Python bindings, so there is no policy service
to run or reach.

```bash
pip install "wardowl[cedar]"
```

```python
from wardowl import PermissionedRetriever
from wardowl.authorizers.cedar import CedarAuthorizer

POLICIES = """
permit (principal, action == Action::"read", resource)
when { resource has allowed_users && resource.allowed_users.contains(principal.id) };

permit (principal, action == Action::"read", resource)
when { resource has allowed_groups && resource.allowed_groups.containsAny(principal.groups) };
"""

retriever = PermissionedRetriever(backend, CedarAuthorizer(POLICIES))
```

## What a policy sees

| Cedar | wardowl |
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
  and that includes a `forbid`. wardowl therefore denies any chunk whose evaluation reported an
  error, and logs how many on the `wardowl` logger. Guard optional attributes with `has`.
- **Values Cedar cannot hold are never left out.** Cedar has strings, booleans, 64-bit integers
  (Long), sets and records. Integers, and floats with no fractional part (`3.0`, from a store that
  returns every number as a float), become Longs. A chunk whose metadata holds any other value (a
  float such as `2.5`, None, an integer beyond 64 bits, or a list or record holding one) is denied
  without asking Cedar, and counted with the errors in the log: left out, the attribute would make
  `resource has x` false and a `forbid` guarded by it would stop applying. A subject attribute of
  that kind raises `AuthorizationError`. Store absent values as absent keys, not None, and numbers
  that need a fraction as strings or scaled integers.
- **Keys Cedar cannot hold are treated the same way.** A key that is not a string (JSON would write
  `1` as `"1"`), or that is `__entity`, `__extn` or `__expr`, which Cedar's JSON reserves for its
  escapes (a record holding one could be read as an entity reference or an extension value), denies
  the chunk wherever it appears in the metadata, and raises `AuthorizationError` in a subject's
  attributes. Other keys that start with `__`, such as `__restricted`, reach Cedar like any other:
  write `resource has "__restricted"` in a policy, and declare them in a `schema` if you pass one.
- **Configuration** is checked when the authorizer is created: policies Cedar cannot parse raise
  `ValueError`. Any other failure raises `AuthorizationError`, and the retriever returns nothing.
