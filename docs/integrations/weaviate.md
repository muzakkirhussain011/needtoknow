# Weaviate

```bash
pip install "needtoknow[weaviate]"
```

The backend runs Weaviate's vector search with the permission filter applied inside it, so the k
nearest chunks the subject may read come back however few of the collection's objects that is. It
needs `weaviate-client` 4.16 or newer. CI runs needtoknow's
[conformance kit](../extending.md#test-it-with-the-conformance-kit) against Weaviate 1.39.12;
Weaviate Cloud serves the same API through the same client, but CI does not test it.

```python
import weaviate

from needtoknow.backends.weaviate import WeaviateBackend, create_collection

client = weaviate.connect_to_local()  # or weaviate.connect_to_weaviate_cloud(...)
create_collection(client, "Chunks")
backend = WeaviateBackend(client, "Chunks", embed=embed)  # embed: str -> list[float]
backend.add(documents)
```

`create_collection` makes a collection with a self-provided vector named `embedding` (HNSW, cosine
distance) and four properties: `needtoknow_id`, `content`, `metadata_json` (the metadata as JSON
text, not indexed) and `needtoknow_terms`. Scores are cosine similarity, 1 minus Weaviate's cosine
distance. For a native `aretrieve`, pass `async_client=` a connected `WeaviateAsyncClient`;
otherwise async calls run in a thread.

## Ids

Weaviate object ids are UUIDs. `add` uses a document id that is already a UUID as it is, derives a
UUID from any other id (`point_id`), and keeps the original in `needtoknow_id`, which results
report.

## How permissions are indexed

The backend uses the same design as [Elasticsearch, OpenSearch](elasticsearch.md#how-permissions-are-indexed),
[Milvus](milvus.md#how-permissions-are-indexed) and [MongoDB](mongodb.md#how-permissions-are-indexed):
one `text[]` property holds a type-tagged term for every string and integer in the chunk's
metadata, so `1` and `"1"` stay apart, and booleans, floats, nested lists and objects never match.

- The property uses `field` tokenization, so each term is one token: no splitting, no lower-casing.
  Each term also starts and ends with the `\x1f` separator, so trimming could never make `"hr "`
  and `"hr"` the same term. CI checks that `"hr "`, `"HR"` and `"hr"` stay apart on a real server.
- A filter becomes `contains_any` conditions joined with `all_of` and `any_of`, built from the
  client's filter objects, so values are always data and never query syntax. An `In` with no values
  matches nothing without a call to Weaviate.
- The metadata is stored as JSON text and returned exactly as written, so the retriever's re-check
  sees the true permissions whatever their shape, and arbitrary metadata never changes the schema.

## Collections written by other tools

An object without `needtoknow_terms` is invisible to a pre-filtered search, though post-filtering
still finds it and checks its metadata. Fill the property in your ingestion pipeline:

```python
import json

from needtoknow.backends.weaviate import permission_terms

properties["needtoknow_terms"] = permission_terms(metadata)
properties["metadata_json"] = json.dumps(metadata)
```
