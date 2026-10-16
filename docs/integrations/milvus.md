# Milvus

```bash
pip install "needtoknow[milvus]"
```

The backend runs Milvus's vector search with the permission filter applied inside the search, so
the k nearest chunks the subject may read come back however few of the collection's chunks that
is. It needs `pymilvus` 2.6 or newer and a Milvus that supports filter templating. CI runs
needtoknow's [conformance kit](../extending.md#test-it-with-the-conformance-kit) against Milvus
3.0.2 standalone. Zilliz Cloud serves the same API through the same client, but CI does not test
it.

```python
from pymilvus import MilvusClient

from needtoknow.backends.milvus import MilvusBackend, create_collection

client = MilvusClient(uri="http://localhost:19530")  # or Zilliz Cloud: uri=..., token=...
create_collection(client, "docs", dim=768)
backend = MilvusBackend(client, "docs", embed=embed)  # embed: str -> list[float]
backend.add(documents)
```

`create_collection` makes a collection with a string primary key (`id`), the vector (`embedding`,
HNSW, cosine), the text (`content`), the metadata (`metadata`, JSON) and the permission terms
(`needtoknow_terms`, with an inverted index). Field names, the metric (`COSINE` or `IP`; scores are
Milvus's similarity, higher is closer) and the HNSW parameters can be changed; pass the same field
names to `MilvusBackend`.

## Tuning

- **`ef`** (default 100) is HNSW's search breadth: higher finds more of the true nearest chunks and
  takes longer. It is raised to `k` when `k` is larger, as Milvus requires.
- **`search_params`** replaces the search parameters entirely, for a collection with another index
  type, for example `{"params": {"nprobe": 16}}` for IVF.
- **`consistency_level`** (default `"Strong"`) makes a chunk searchable as soon as `add` returns.
  `"Bounded"` is faster when a short delay after writes is acceptable; it never affects which
  chunks a subject may read, only how soon new ones appear.
- For a native `aretrieve`, pass `async_client=AsyncMilvusClient(...)`; otherwise async calls run
  in a thread.

## How permissions are indexed

The backend uses the same design as [Elasticsearch and OpenSearch](elasticsearch.md#how-permissions-are-indexed):
one array field, `needtoknow_terms`, holds a type-tagged term for every string and integer in the
chunk's metadata, so `1` and `"1"` stay apart and booleans, floats, nested lists and objects never
match. A filter becomes `ARRAY_CONTAINS_ANY` conditions on that field, joined with `AND` and `OR`,
and an `In` with no values matches nothing without a call to Milvus.

**Values never enter the expression.** Milvus filters are text expressions. needtoknow writes only
placeholders into the expression and passes the values separately, as Milvus's
[filter templating](https://milvus.io/docs/filtering-templating.md) (`filter_params`) allows:

```python
from needtoknow import In
from needtoknow.backends.milvus import filter_to_expression

filter_to_expression(In("allowed_groups", ["hr", "eng"]))
# ('ARRAY_CONTAINS_ANY(needtoknow_terms, {p0})', {'p0': ['allowed_groups\x1fs:eng', 'allowed_groups\x1fs:hr']})
```

A group name or user id containing quotes, brackets or operators is therefore only ever a value, and
cannot change what the filter selects. CI checks this against a real Milvus.

The metadata itself is stored in the JSON field and returned as written, so the retriever's
re-check sees the true permissions. The terms field is not consulted by the re-check: if the two
ever disagree, a chunk can be hidden from a pre-filtered search, never leaked.

## Limits

- `max_terms` (default 4,096) caps the tagged terms per chunk, one per string or integer in its
  metadata; a chunk shared with more groups than that needs a larger value, within what your Milvus
  allows for an array field. Inserting a chunk with more fails, so nothing is stored with partial
  permissions.
- `max_term_length` (default 1,024) caps one term: the field name, two characters and the value.
- `max_content_length` (default 65,535, Milvus's largest `VARCHAR`) caps the chunk text.
- `max_id_length` (default 512) caps document ids.

## Collections written by other tools

A row whose `needtoknow_terms` is empty is invisible to a pre-filtered search, though post-filtering
still finds it and checks its metadata. Fill the field in your ingestion pipeline with
`permission_terms`:

```python
from needtoknow.backends.milvus import permission_terms

row = {"id": doc_id, "embedding": vector, "content": text, "metadata": metadata}
row["needtoknow_terms"] = permission_terms(metadata)
client.upsert(collection_name="docs", data=[row])
```
