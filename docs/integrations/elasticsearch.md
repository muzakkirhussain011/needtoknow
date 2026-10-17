# Elasticsearch and OpenSearch

```bash
pip install "needtoknow[elasticsearch]"   # or "needtoknow[opensearch]"
```

Both backends run an approximate kNN search with the permission filter applied *during* the
search, so the k nearest chunks the subject may read come back however few of the index's
documents that is. Elasticsearch 8.12 or newer and OpenSearch 2.9 or newer are supported; CI runs
needtoknow's [conformance kit](../extending.md#test-it-with-the-conformance-kit) against
Elasticsearch 9.5.5 and OpenSearch 3.9.0.

## Elasticsearch

```python
from elasticsearch import Elasticsearch

from needtoknow.backends.elasticsearch import ElasticsearchBackend, create_index

client = Elasticsearch("http://localhost:9200")
create_index(client, "docs", dim=768)
backend = ElasticsearchBackend(client, "docs", embed=embed)  # embed: str -> list[float]
```

The filter goes in the `knn` clause's `filter`. Elasticsearch documents that this filter "is
applied during approximate kNN search to ensure that k matching documents are returned"; a filter
placed beside the `knn` clause would apply afterwards and could return fewer, so needtoknow never
does that. `num_candidates=` (default 100, at least k, at most 10,000) is how many neighbours each
shard considers: higher finds more of the true nearest chunks and takes longer.

Elasticsearch quantizes dense vectors by default (`int8_hnsw` below 384 dimensions, `bbq_hnsw`
from 384 in 9.1 and later). That saves memory and costs some recall; pass
`create_index(..., index_options={"type": "hnsw"})` to keep full-precision vectors. On a million
384-dimensional chunks, the default with 100 candidates found 0.62 of the true top 10 for a user
who may read 1% of them; `int8_hnsw` with `num_candidates=500` found 0.98 at a similar median
latency (11 ms against 13), in the same 2.2 GiB, and full-precision `hnsw` found 1.00
([Benchmarks](../benchmarks.md#every-store-on-the-same-million-chunks)). For a native
`aretrieve`, pass `async_client=AsyncElasticsearch(...)` (install `elasticsearch[async]`).

## OpenSearch

```python
from opensearchpy import OpenSearch

from needtoknow.backends.opensearch import OpenSearchBackend, create_index

client = OpenSearch(hosts=["http://localhost:9200"])
create_index(client, "docs", dim=768)  # engine="lucene" (default) or "faiss"
backend = OpenSearchBackend(client, "docs", embed=embed)
```

The filter goes in the `knn` query's `filter`, which the Lucene and Faiss engines apply during the
search (efficient k-NN filtering). `create_index` makes a `knn_vector` field with cosine similarity
and HNSW; pass `parameters={"m": 16, "ef_construction": 128}` to tune the graph. For a native
`aretrieve`, pass `async_client=AsyncOpenSearch(...)` (install `opensearch-py[async]`).

## How permissions are indexed

Keyword fields in both engines hold strings, so a `terms` query for the integer `1` also matches the
string `"1"`, and a field's type is fixed by the first document that used it. The reference
semantics keep `1` and `"1"` apart and never match booleans or floats. So both backends index one
keyword field, `needtoknow_terms`, with a type-tagged term for every string and integer in the
chunk's metadata, scalars and list elements alike:

| Metadata | `needtoknow_terms` |
|---|---|
| `{"allowed_groups": ["hr", "eng"]}` | `allowed_groups␟s:eng`, `allowed_groups␟s:hr` |
| `{"level": 1}` | `level␟i:1` |
| `{"level": "1"}` | `level␟s:1` |
| `{"public": true, "score": 0.9}` | none: no filter ever matches booleans or floats |

(`␟` is the ASCII unit separator, `\x1f`; field names may not contain it.) A filter becomes a
`terms` query on these tags, `And` a `bool` `filter`, `Or` a `bool` `should`, and an `In` with no
values matches nothing without a call to the engine. The metadata itself is stored with
`enabled: false`: kept in `_source` and returned exactly as written, so the retriever's re-check
sees the true permissions, but never indexed, so arbitrary metadata cannot add mappings to the
index. The index uses `dynamic: false` for the same reason.

## Indexes written by other tools

A document without `needtoknow_terms` is invisible to a pre-filtered search: its metadata may say it
is readable, but no tag matches. Post-filtering still finds it and checks its metadata. To make such
an index fully searchable, add the field to each document in your ingestion pipeline:

```python
from needtoknow.backends.elasticsearch import TERMS_FIELD, permission_terms

document[TERMS_FIELD] = permission_terms(document["metadata"])
```

and map `needtoknow_terms` as `keyword`. A document without a `metadata` object carries no
permissions, so the re-check denies it.

## Limits

Both engines cap k at 10,000. A `terms` query holds at most `index.max_terms_count` values (65,536
by default), which bounds how many groups a single subject's filter can name; see
[Operations](../operations.md#subjects-in-very-many-groups).
