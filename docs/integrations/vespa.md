# Vespa

[Vespa](https://vespa.ai) serves vector search, BM25 and ranking at scale, self-hosted or on Vespa
Cloud. `VespaBackend` writes through Vespa's `/document/v1` API and searches through `/search/`,
with the permission filter in the YQL query, so Vespa applies it inside the nearest-neighbor or
BM25 search: pre-filtering is exact.

```bash
pip install "wardowl[vespa]"
```

The application package needs a schema with the fields the backend writes. `schema(dim)` writes
one; [`examples/vespa`](https://github.com/muzakkirhussain011/wardowl/tree/main/examples/vespa)
is a complete single-node package, which CI deploys.

```python
from pathlib import Path

from wardowl import MetadataACLAuthorizer, PermissionedRetriever
from wardowl.backends.vespa import VespaBackend, schema

Path("app/schemas/wardowl.sd").write_text(schema(768))  # then deploy the package

backend = VespaBackend("http://localhost:8080", embed)
backend.add(chunks)  # or backend.add(chunks, embeddings) with vectors you computed
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
```

## How filtering works

Each chunk stores a tagged term for every string or integer in its metadata (the field name, the
value's type and the value), hex-encoded, in the array attribute `wardowl_terms` with fast-search.
A filter becomes `wardowl_terms in ("...")` conditions joined with `and` and `or`. The terms keep
`1` and `"1"` apart, as the reference semantics do, and every value the filter writes into the YQL
text is hex digits, so no metadata value can change the query. The metadata itself is stored as
JSON and returned exactly as written. A filter that can match nothing returns no hits without a
query, and `filter_to_yql` raises `NothingMatches` for it, since a query without the condition
matches every chunk.

## Search modes

- `approximate=True` (the default) searches the HNSW index with the filter applied during the
  search; Vespa switches to exact search when the filter admits few chunks.
- `approximate=False` compares the query with every chunk the filter admits.
- `mode="keyword"` ranks by BM25 on the text, under the same filter. With `RankFusion`, a vector
  backend and a keyword backend on the same schema give [hybrid search](../hybrid.md).

Scores are cosine similarities (`cos(distance(field, embedding))` with the angular metric), or BM25
scores in keyword mode.

## Schemas, namespaces and tenants

`schema=` (`wardowl` by default) is the schema the backend writes to and searches;
`schema(dim, name=...)` writes a schema of that name. `namespace=` (`wardowl` by default) is only
the namespace part of each document's path, `/document/v1/<namespace>/<schema>/docid/<id>`, which
`add`, `set_metadata` and `delete` use. A search selects from the whole schema
(`select * from <schema>`), whatever its namespace. Two backends with different namespaces on one
schema therefore search each other's chunks, and both report a chunk by the id it was added with,
so an id written in both is returned twice under one name.

To keep tenants apart in one schema, filter by tenant: put the tenant on every chunk and use
`AllOf(TenantAuthorizer(), ...)` ([Combining](../combining.md#tenant-isolation)). Or give each tenant
its own schema, listed under the content cluster's `<documents>` in `services.xml` (as
`<document type="acme" mode="index"/>`):

```python
Path("app/schemas/acme.sd").write_text(schema(768, name="acme"))  # then deploy the package
acme = VespaBackend("http://localhost:8080", embed, schema="acme")
```

## Production notes

- **Vespa Cloud and TLS:** pass `client=httpx.Client(cert=..., headers=...)` for client certificates,
  tokens or proxies; the backend uses it as given.
- **Hits per query:** Vespa allows 400 hits unless the query profile raises `maxHits`, as the example
  package does. Post-filtering can ask for more than `k`.
- **Coverage:** a search that did not cover every document (a content node down or slow, or a
  timeout: `coverage.full` false, or `coverage.degraded` set) raises `RuntimeError` rather than
  return its partial results as complete.
- **Writes** go `concurrency` requests at a time (8 by default). A write that fails raises; repeated
  ids in one `add` keep the last.
- **Ids:** a chunk id goes into the `/document/v1` path percent-encoded, `/` included. The ids `.`
  and `..` are refused with `ValueError` by `add`, `set_metadata` and `delete`, before any request:
  HTTP clients drop such a path segment, which would send the request to another path, and Vespa
  refuses them percent-encoded too.
- **Visibility:** a write is searchable when Vespa acknowledges it, so `add` returning means the chunk
  is searchable.
