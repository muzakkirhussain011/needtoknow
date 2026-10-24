# Combining authorizers and multi-tenancy

Real permission models have layers: a SaaS product must never return one customer's documents to
another, and inside a customer, documents are shared through groups, an OpenFGA model or a policy.
`AllOf`, `AnyOf` and `TenantAuthorizer` compose those layers without writing your own authorizer.

## Tenant isolation

```python
from wardowl import AllOf, PermissionedRetriever, Subject, TenantAuthorizer
from wardowl.authorizers.openfga import OpenFGAAuthorizer

authorizer = AllOf(TenantAuthorizer(), OpenFGAAuthorizer(url, store_id))
retriever = PermissionedRetriever(backend, authorizer)

subject = Subject(user.id, user.groups, {"tenant": user.tenant_id})
result = retriever.retrieve(question, subject, k=5)
```

`TenantAuthorizer(field="tenant", attribute=None)` allows a chunk only when its metadata field
`field` holds the subject's attribute `attribute`, which is the field's own name when left as
`None`: `TenantAuthorizer(field="org_id", attribute="org")` compares the chunk's `org_id` with the
subject's `org`. Put the tenant id on every chunk at ingestion. A subject without the attribute, or
whose attribute is empty or not a string or integer, raises `AuthorizationError`: a request that
lost its tenant returns nothing, never everything.

One chunk can serve several tenants: store a list, `{"tenant": ["acme", "globex"]}`, and the chunk
matches the subjects of either, because `In` matches a list field by any of its elements, in the
check and in every backend's filter. A subject belongs to one tenant at a time.

## AllOf and AnyOf

| | A chunk is readable when | Pre-filter | Scope |
|---|---|---|---|
| `AllOf(a, b, ...)` | every authorizer allows it | `And` of their filters, if all have one | `And` of the filters that exist |
| `AnyOf(a, b, ...)` | at least one allows it | `Or` of their filters, if all have one | `Or` of each one's filter or scope, if all have one |

Authorizers are asked in order. `AllOf` asks each one only about the chunks every earlier one
allowed, and `AnyOf` only about the chunks every earlier one denied, so put the cheap, local ones
first and remote services last: with `AllOf(TenantAuthorizer(), OpenFGAAuthorizer(...))`, OpenFGA
is never asked about another tenant's chunks. Every part must answer well: a wrong number of
decisions or a non-boolean from any of them raises `AuthorizationError`.

```python
from wardowl import AnyOf, MetadataACLAuthorizer
from wardowl.authorizers.opa import OPAAuthorizer

# Shared directly on the document, or allowed by policy.
authorizer = AnyOf(MetadataACLAuthorizer(), OPAAuthorizer(opa_url, filter_path="wardowl/filter"))
```

## Scoped post-filtering

When the combination has an exact filter, the retriever pre-filters as usual. When one part has
none (a `FunctionAuthorizer` with only `check`, or OpenFGA when its list of documents may be
truncated), `AllOf` cannot give an exact filter. It can still give a **scope**: the conjunction of
the filters that do exist, which every readable chunk matches. The retriever then post-filters
*inside the scope*: it searches only the subject's tenant and checks every candidate. It asks for
the scope whenever it post-filters, `strategy="postfilter"` included, so when every part has a
filter the store receives the same filter it would when pre-filtering
([What the store receives](concepts.md#what-the-store-receives)).

That matters at scale. If a tenant holds 1% of the index, post-filtering across the whole index
examines about a hundred chunks of other tenants for every one of its own and, within
`max_candidates`, finds little or nothing; post-filtering inside the tenant examines only its
chunks. Results are exactly right either way, because every candidate is still checked.

Any authorizer can offer a scope by implementing `scope_for(subject, action) -> Filter | None`;
`CachingAuthorizer` caches it like `filter_for`. `AllOf` computes its scope by asking each part for
its filter again, so wrap remote parts in `CachingAuthorizer` to keep that to one call.

## Testing a combination

```python
from wardowl.testing import check_authorizer

check_authorizer(AllOf(TenantAuthorizer(), MetadataACLAuthorizer()), subjects, hits)
```

checks that the combined `check` and `filter_for` agree for your subjects and chunks.
