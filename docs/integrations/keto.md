# Ory Keto

[Ory Keto](https://www.ory.sh/keto/) stores relationships in the style of Google's Zanzibar:
`Document:plan#view@ann`, or `Document:pay#view@(Group:hr#members)` for everyone in a group.
`KetoAuthorizer` asks Keto whether the subject may view each chunk's document. It needs no extra
dependency.

```python
from needtoknow import PermissionedRetriever
from needtoknow.authorizers.keto import KetoAuthorizer

authorizer = KetoAuthorizer("http://keto:4466")  # Keto's read API
retriever = PermissionedRetriever(backend, authorizer)
```

## How chunks map to Keto

| needtoknow | Keto |
|---|---|
| The chunk's document | Object `metadata["doc_id"]` in namespace `Document` (`resource_field`, `namespace`); a chunk without it is denied |
| `read` | Relation `view` (`actions={"read": "view"}`) |
| The subject | `subject_id=<subject.id>`; Keto follows subject sets such as `Group:hr#members` itself |

Checks go to `POST /relation-tuples/batch/check`, `batch_size` tuples at a time (100 by default).

## Groups from the token

When group memberships live in your identity provider rather than in Keto, `group_subjects=True`
also checks the subject set `Group:<group>#members` for each of the subject's groups, and allows a
chunk if any check does. The groups are then trusted as given, so they must come from the verified
token, as [Security model](../security.md#the-subject-must-come-from-authentication) explains.

## Pre-filtering

Keto has no reverse lookup ("which documents may ann view?"), so `filter_for` returns None and the
retriever post-filters: it searches, then checks each candidate, widening the search until it has
k permitted chunks or reaches `max_candidates`. For users who may read little of a large corpus,
combine Keto with a filter the store can apply, such as `AllOf(TenantAuthorizer(), KetoAuthorizer(...))`,
so the search starts inside the subject's tenant.

## Failures

Every failure raises `AuthorizationError`, and the retriever returns nothing: an unreachable server,
a malformed answer, a wrong number of results, or a result that carries an error (an unknown
namespace, for example). Connections are reused, transient failures are retried once, and a
`CircuitBreaker` can pause requests to a failing server.

`examples/keto/keto.yml` and `examples/keto/namespaces.ts` are the configuration CI uses: an
in-memory store whose `Document` namespace grants `view` to users and to the members of groups.
