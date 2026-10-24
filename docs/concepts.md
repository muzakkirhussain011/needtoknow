# Concepts

wardowl has four parts: the **subject** who asks, the **backend** that searches, the
**authorizer** that decides, and the **retriever** that combines them and enforces the
guarantees.

## Subjects

A `Subject` is who is asking: a user id, the groups they belong to, and any attributes your
policy needs.

```python
from wardowl import Subject

alice = Subject("alice", groups=["hr", "managers"], attributes={"region": "emea"})
```

The id must not be empty, and `groups` must be a collection: `Subject("bob", "eng")` raises
instead of quietly becoming the groups `e`, `n` and `g`. Subjects are immutable and hashable,
which is what lets [`CachingAuthorizer`](operations.md#permission-caching) key its cache by
them.

## Documents and hits

A `Document` is a chunk as stored: an id, its text and its metadata. A `Hit` is a search result:
the same fields plus the backend's relevance score, where higher is better. Permissions usually
live in the metadata, for example `{"allowed_groups": ["hr"]}` or `{"doc_id": "handbook-2026"}`
for a permission system that decides per source document.

## Authorizers

An authorizer answers two questions:

`check(subject, hits, action) -> list[bool]`
: May the subject see each of these hits? One boolean per hit, in order. Every authorizer must
  answer this.

`filter_for(subject, action) -> Filter | None`
: A filter that selects exactly what the subject may see, or `None` when the rules cannot be
  written as one. When it returns a filter, the vector store can filter inside the search.

Any doubt must raise. The retriever turns every exception from an authorizer into an
`AuthorizationError` and returns nothing.

| Authorizer | Decides with | Pre-filters |
|---|---|---|
| `MetadataACLAuthorizer` | `allowed_users` and `allowed_groups` on each chunk (`"*"` for everyone) | Yes |
| `FunctionAuthorizer` | Your function, optionally with your own `filter_for` | If you give one |
| [`OPAAuthorizer`](integrations/opa.md) | A Rego policy in Open Policy Agent | If the policy returns a filter |
| [`OpenFGAAuthorizer`](integrations/openfga.md) | Relationship tuples in OpenFGA | Yes, through ListObjects |
| [`CachingAuthorizer`](operations.md#permission-caching) | Any of the above, cached for a bounded time | As the wrapped one |

## Filters

Filters are a small language every backend can translate: `In`, `And` and `Or`.

```python
from wardowl import And, In, Or

f = Or(
    In("allowed_users", ["alice"]),
    And(In("allowed_groups", ["hr", "*"]), In("region", ["emea"])),
)
```

`In(field, values)` is true when the metadata field equals one of the values, or, for a list
field such as `allowed_groups: ["sales", "hr"]`, when the two share an element. The rules are
strict on purpose, because a permission filter that is too generous is a leak:

- A missing field never matches. Missing permissions mean no access.
- Values are strings or integers only. Metadata values of other types (booleans, floats, nested
  objects) never match. Python treats `True == 1`, JSON stores compare by type, and keeping to two
  types gives every backend the same answer.
- `And()` and `Or()` with no clauses are rejected. An empty `And` would be true for everything.
- `In(field, [])` matches nothing.

`matches(f, metadata)` is the reference semantics. Every backend's translation is tested against
it. `filter_to_dict` and `filter_from_dict` convert filters to and from JSON, which is how
`OPAAuthorizer` receives them from a policy.

## Strategies

`PermissionedRetriever(backend, authorizer, strategy=...)` decides where permissions are applied.

| Strategy | When it is used | Cost | Result |
|---|---|---|---|
| `prefilter` | The authorizer returns a filter and the backend supports filters | One search | The exact top-k of what the subject may read, as far as the index is exact |
| `postfilter` | Any authorizer and backend | `k × overfetch` candidates, widening up to `max_candidates` | Exact when enough permitted candidates are found; `complete=False` otherwise |
| `auto` (default) | `prefilter` when both sides support it, else `postfilter` | | |

**Pre-filtering** puts the filter inside the search. The vector store only ranks chunks the subject
may read, so a subject who may read 0.1% of the corpus still gets ten good results, not the
permitted few among the global top ten.

**Post-filtering** searches without a filter and checks the candidates. It starts with
`k × overfetch` candidates (default 4), and while fewer than k are permitted it multiplies the
fetch by `overfetch` and searches again, until k are found, the backend runs out, or
`max_candidates` (default 1000) is reached. The last case is reported as `complete=False`, so you
know the result may be missing permitted chunks rather than finding out from users. Subjects who
may read only a small share of the corpus are where post-filtering struggles; the
[benchmarks](benchmarks.md) show by how much.

Choose `prefilter` explicitly when you want an error rather than a silent fallback if a filter is
not available.

## What a result tells you

| Field | Meaning |
|---|---|
| `hits` | Permitted chunks, best first, at most `k` |
| `strategy` | The strategy that actually ran |
| `complete` | `False` when wardowl knows results may be missing (see below) |
| `examined` | Candidates the authorizer checked |
| `denied` | Post-filtered candidates the subject may not read |
| `violations` | Pre-filtered hits the re-check rejected |

`violations` should always be 0. Anything else means the backend returned chunks outside the
filter, and deserves an alert: the re-check removed them, but the backend has a bug.

`complete=False` means wardowl knows results may be missing: post-filtering stopped at
`max_candidates`, or the re-check removed hits. `complete=True` trusts the backend's ranking. An
approximate index (HNSW and similar) can return fewer or different results than an exact search,
and no caller can tell that apart from "the user may read fewer than k chunks". How much each
backend misses is measured in the [benchmarks](benchmarks.md); for pgvector, keep iterative index
scans on (the default), which is what keeps a selective filter from cutting results short.

## The guarantees

**Fail closed.** If the authorizer raises, times out, returns the wrong number of decisions, or
returns anything that is not a real `bool` (`"no"`, `1`, `None`), the call raises
`AuthorizationError`. There is no fallback to unfiltered results.

**Defence in depth.** With `recheck=True` (the default), pre-filtered hits are checked again with
`authorizer.check`. A backend that ignores or mistranslates the filter cannot leak; the rejected
hits are dropped and counted in `violations`. With `recheck=False` the authorizer is not asked
again, but each pre-filtered hit is still matched against the filter in the process: a backend
that ignores or mistranslates the filter still cannot leak, though a filter broader than the
authorizer's own check would go unnoticed.

**One algorithm, two drivers.** `retrieve` and `aretrieve` run the same plan, written once, so the
async path cannot drift from the sync one. Backends and authorizers may provide `asearch`,
`afilter_for` and `acheck`; otherwise their sync methods run in a thread.

These guarantees are tested as properties with [Hypothesis](https://hypothesis.readthedocs.io):
for random corpora, permissions, subjects and queries, no strategy may return a chunk the subject
may not read, results must equal an independent oracle's top-k, and a backend that ignores the
filter must still not leak.
