# Vespa

[Vespa](https://vespa.ai) serves vector search, BM25 and ranking at scale, self-hosted or on Vespa
Cloud. `VespaBackend` writes through Vespa's `/document/v1` API and searches through `/search/`,
with the permission filter in the YQL query, so Vespa applies it inside the nearest-neighbor or
BM25 search: pre-filtering is exact.

```bash
pip install "needtoknow[vespa]"
```

The application package needs a schema with the fields the backend writes. `schema(dim)` writes
one; [`examples/vespa`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples/vespa)
is a complete single-node package, which CI deploys.

```python
from pathlib import Path

from needtoknow import MetadataACLAuthorizer, PermissionedRetriever
from needtoknow.backends.vespa import VespaBackend, schema

Path("app/schemas/needtoknow.sd").write_text(schema(768))  # then deploy the package

backend = VespaBackend("http://localhost:8080", embed)
backend.add(chunks)  # or backend.add(chunks, embeddings) with vectors you computed
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
```

## How filtering works

Each chunk stores a tagged term for every string or integer in its metadata (the field name, the
value's type and the value), hex-encoded, in the array attribute `needtoknow_terms` with fast-search.
A filter becomes `needtoknow_terms in ("...")` conditions joined with `and` and `or`. The terms keep
`1` and `"1"` apart, as the reference semantics do, and every value the filter writes into the YQL
text is hex digits, so no metadata value can change the query. The metadata itself is stored as
JSON and returned exactly as written.

## Search modes

- `approximate=True` (the default) searches the HNSW index with the filter applied during the
  search; Vespa switches to exact search when the filter admits few chunks.
- `approximate=False` compares the query with every chunk the filter admits.
- `mode="keyword"` ranks by BM25 on the text, under the same filter. With `RankFusion`, a vector
  backend and a keyword backend on the same schema give [hybrid search](../hybrid.md).

Scores are cosine similarities (`cos(distance(field, embedding))` with the angular metric), or BM25
scores in keyword mode.

## Production notes

- **Vespa Cloud and TLS:** pass `client=httpx.Client(cert=..., headers=...)` for client certificates,
  tokens or proxies; the backend uses it as given.
- **Hits per query:** Vespa allows 400 hits unless the query profile raises `maxHits`, as the example
  package does. Post-filtering can ask for more than `k`.
- **Writes** go `concurrency` requests at a time (8 by default). A write that fails raises; repeated
  ids in one `add` keep the last.
- **Visibility:** a write is searchable when Vespa acknowledges it, so `add` returning means the chunk
  is searchable.
