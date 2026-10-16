# PostgreSQL + pgvector

```bash
pip install "needtoknow[pgvector]"
```

Needs PostgreSQL with pgvector 0.8 or newer. The permission filter runs inside the SQL query, so
the database returns the nearest chunks the subject may read, however few of the table's rows that
is.

## Set up

```python
from psycopg_pool import ConnectionPool

from needtoknow.backends.pgvector import PgVectorBackend, create_schema

pool = ConnectionPool("postgresql://localhost/app")
create_schema(pool, dim=768)
backend = PgVectorBackend(pool, embed=embed)  # embed: str -> list[float], your embedding model
```

`create_schema(pool, dim, table="needtoknow_chunks", hnsw=True)` creates, if they do not exist:

- the `vector` extension;
- a table with `id text PRIMARY KEY`, `content text`, `metadata jsonb` and `embedding vector(dim)`;
- a GIN index on `metadata` (`jsonb_path_ops`), which serves the permission filter;
- an HNSW index on `embedding` with cosine distance (pass `hnsw=False` to create it yourself, for
  example after a bulk load).

To use an existing table, give it those four columns and pass `table=`.

## Write and delete

```python
from needtoknow import Document

backend.add([Document("salaries-1", "Salary bands for 2026 ...", {"allowed_groups": ["hr"], "doc_id": "salaries"})])
backend.add(documents, embeddings=vectors)  # if you computed the embeddings already
backend.delete(["salaries-1"])
```

`add` inserts or replaces by id, so re-indexing a document after its permissions change is the
same call.

## How the filter becomes SQL

Each value in an `In` becomes two JSON containment tests, one for a scalar field and one for a
list field:

```sql
metadata @> '{"allowed_groups": "hr"}'::jsonb OR metadata @> '{"allowed_groups": ["hr"]}'::jsonb
```

`And` and `Or` become `AND` and `OR`. Values are bound parameters, never interpolated. Containment
compares by type, which matches the reference semantics, and the GIN index serves it.

## Iterative index scans

An HNSW index normally returns a fixed number of nearest rows (`hnsw.ef_search`, 40 by default)
and the filter is applied to those. With a selective filter, few or none of them pass, and the
query returns fewer than k rows. pgvector 0.8 added iterative index scans, which keep scanning
until enough rows pass. The backend turns them on for each query (`SET LOCAL
hnsw.iterative_scan`):

| `iterative_scan=` | Behaviour |
|---|---|
| `"relaxed_order"` (default) | Keeps scanning; results can be slightly out of order, so the backend re-sorts them by distance |
| `"strict_order"` | Keeps scanning in exact distance order; slower |
| `None` | Off, as before pgvector 0.8 |

Results are ordered by distance, then by id, so equal scores come back in a stable order. Scores
are cosine similarity, `1 - cosine distance`.

## Async

Pass an `AsyncConnectionPool` as `async_pool=` and `aretrieve` uses a native async search.
Without one, async calls run `search` in a thread.
