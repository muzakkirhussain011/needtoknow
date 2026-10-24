# Cerbos

Policy-based access control with [Cerbos](https://www.cerbos.dev), through the Cerbos PDP's HTTP
API. No extra dependency: requests use the standard library.

```bash
docker run -p 3592:3592 -v "$PWD/policies:/policies" ghcr.io/cerbos/cerbos:0.56.0
```

```python
from wardowl.authorizers.cerbos import CerbosAuthorizer

authorizer = CerbosAuthorizer("http://localhost:3592")
```

## What Cerbos sees

Each chunk is a resource of kind `document` (change it with `resource_kind=`) whose attributes are
the chunk's metadata, and whose id is the chunk id (or the metadata field `resource_field=`). The
subject is the principal:

```json
{"id": "alice", "roles": ["user"], "attr": {"groups": ["eng", "hr"], "region": "eu"}}
```

`roles=` is a list of roles for every principal, or a function of the subject; `attr` holds the
subject's groups and its `attributes`. A subject attribute named `groups` is refused with
`AuthorizationError`, since it would replace the subject's groups. Pass `policy_version=` and
`scope=` to select a policy version and scope, and `actions={"read": "view"}` to map wardowl's
action to yours.

A policy for permissions stored on the chunks
([`examples/cerbos/policies/document.yaml`](https://github.com/muzakkirhussain011/wardowl/blob/main/examples/cerbos/policies/document.yaml)):

```yaml
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: default
  resource: document
  rules:
    - actions: ["read"]
      effect: EFFECT_ALLOW
      roles: ["*"]
      condition:
        match:
          any:
            of:
              - expr: request.principal.id in request.resource.attr.allowed_users
              - expr: '"*" in request.resource.attr.allowed_groups'
              - expr: hasIntersection(request.resource.attr.allowed_groups, request.principal.attr.groups)
```

## Checks

`check` sends CheckResources requests of `batch_size` resources (50, Cerbos's default limit per
request) and reads `EFFECT_ALLOW` or `EFFECT_DENY` for each. An answer for a different resource, a
missing or unknown effect, or a wrong number of results raises `AuthorizationError`.

## Pre-filtering from query plans

`filter_for` asks PlanResources which documents the principal may read. Cerbos answers with a query
plan: always allowed, always denied, or a condition over resource attributes, with the principal's
attributes already filled in. wardowl translates the parts of the plan it can express exactly:

| Condition | Filter |
|---|---|
| `and`, `or` | `And`, `Or` |
| `hasIntersection(request.resource.attr.F, ["a", "b"])` | `In(F, ["a", "b"])` |
| `"a" in request.resource.attr.F` (membership in a list field) | `In(F, ["a"])` |
| `request.resource.attr.F == "a"`, `request.resource.attr.F in ["a", "b"]` | `In(F, ...)` |
| always denied | a filter that matches nothing |

Anything else (`!=`, `not`, comparisons, `exists` and other macros, nested attribute paths, boolean
or float values) and "always allowed" give no filter, and the retriever post-filters, with Cerbos
deciding each candidate. Results stay correct either way; pre-filtering is what keeps recall high
for users who may read little, so write the rules that decide reading in the subset above.

CEL's `==` compares a list attribute as a whole, and `in` or `hasIntersection` on an attribute
holding one value is a CEL error, so Cerbos denies; wardowl's `In` matches any element of a list
and a single value alike. Keep attributes compared with `==` single values, and attributes read
with `hasIntersection` or `in` lists, such as `allowed_groups`. A chunk of the other shape passes
the filter but not `check`: the re-check of pre-filtered hits (`recheck=True`, the default) drops
it and reports it in `violations`. With `PermissionedRetriever(recheck=False)` the filter alone
decides and such a chunk is returned, so use `recheck=False` only when every chunk stores each
attribute in the shape the policy assumes.

## Failures

Every failure raises `AuthorizationError`, so retrieval returns nothing. `timeout=` (2 seconds by
default) bounds the whole call, and an answer larger than `max_response_bytes=` (10 MiB by default)
is refused. An `Authorization` header over `http://` to another machine needs
`allow_insecure=True`, as for [OPA](opa.md#failing-closed). Connections are reused, transient
failures are retried once, and a `CircuitBreaker` can pause requests to a failing PDP (see
[Operations](../operations.md#resilience-of-remote-permission-services)).

CI runs the authorizer against Cerbos 0.56.0 with the example policy: the policy yields a
pre-filter, every strategy returns exactly the reference answer for random documents, and `check`
and `filter_for` pass the [conformance kit](../extending.md#test-it-with-the-conformance-kit).
