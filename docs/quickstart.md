# Getting started

## Install

```bash
pip install wardowl
```

Python 3.11 or newer: every Python version CPython still supports, each dropped in the first minor
release after its end of life. The core has no dependencies; each integration is an optional extra, for
example `pip install "wardowl[pgvector]"`.

## A first permissioned search

Chunks carry their permissions in metadata. Here `MetadataACLAuthorizer` reads them from the
`allowed_groups` field, where `"*"` means everyone:

```python
from wardowl import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]}),
        Document("runbook", "Roll back a failed deploy ...", {"allowed_groups": ["engineering"]}),
    ]
)
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())

bob = Subject("bob", groups=["engineering"])
result = retriever.retrieve("leave salary bands", bob, k=3)

print([hit.id for hit in result.hits])  # ['handbook']
print(result.strategy, result.complete)  # prefilter True
```

The salary document matched the query better than the handbook, but Bob is not in `hr`, so it is
never returned. `InMemoryBackend` ranks by keyword overlap and is meant for tests and examples;
real deployments use a vector store.

What came back:

| Field | Meaning |
|---|---|
| `hits` | The permitted chunks, best first, at most `k` |
| `strategy` | `prefilter` (the filter ran inside the search) or `postfilter` (candidates were checked after it) |
| `complete` | `False` when results may be missing, for example because post-filtering reached its candidate limit |
| `examined`, `denied` | Candidates the authorizer checked, and how many of them Bob may not read |
| `violations` | Pre-filtered hits the re-check rejected. Always 0 unless the backend ignored the filter |

## Async

`aretrieve` takes the same arguments and gives the same guarantees:

```python
import asyncio


async def main():
    result = await retriever.aretrieve("leave salary bands", bob, k=3)
    print([hit.id for hit in result.hits])


asyncio.run(main())
```

## Connect a vector store

With PostgreSQL and pgvector (`pip install "wardowl[pgvector]"`):

```python
from psycopg_pool import ConnectionPool

from wardowl import Document, MetadataACLAuthorizer, PermissionedRetriever, Subject
from wardowl.backends.pgvector import PgVectorBackend, create_schema

pool = ConnectionPool("postgresql://localhost/app")
create_schema(pool, dim=768)  # the table, a GIN index on metadata and an HNSW index
backend = PgVectorBackend(pool, embed=embed)  # embed: your model, str -> list[float]
backend.add([Document("salaries", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]})])

retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
result = retriever.retrieve("What are the salary bands?", Subject("alice", groups=["hr"]))
```

The permission filter runs inside the SQL query, so the result is the nearest chunks Alice may
read, however few of the table's rows that is.

## Where the subject comes from

Build the `Subject` from your authenticated session: the user id and groups your identity
provider vouches for. Never take them from the request body, and never let a language model
choose them, for example as a tool argument. See [Security model](security.md).

## Next

- [Concepts](concepts.md) explains strategies, filters and the guarantees.
- [Integrations](integrations/index.md) covers each vector store, permission system and framework.
- [Operations](operations.md) covers caching, audit logs and tracing.
