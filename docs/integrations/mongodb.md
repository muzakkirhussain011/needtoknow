# MongoDB Atlas Vector Search

```bash
pip install "needtoknow[mongodb]"
```

The backend runs `$vectorSearch` with the permission filter as its pre-filter, so the k nearest
chunks the subject may read come back however few of the collection's chunks that is. It works on
MongoDB Atlas and on local Atlas deployments (the `mongodb/mongodb-atlas-local` image, or
`atlas deployments setup --type local`). CI runs needtoknow's
[conformance kit](../extending.md#test-it-with-the-conformance-kit) against
`mongodb/mongodb-atlas-local:8.0.30`; Atlas itself serves the same `$vectorSearch` stage, but CI
does not test it.

```python
from pymongo import MongoClient

from needtoknow.backends.mongodb import MongoDBBackend, create_search_index

collection = MongoClient(MONGODB_URI)["rag"]["chunks"]
create_search_index(collection, dim=768)  # waits until the index is queryable
backend = MongoDBBackend(collection, embed=embed)  # embed: str -> list[float]
backend.add(documents)  # waits until the index shows them
```

Each chunk is one document: `_id` (the document id), `content`, `metadata`, `embedding`, and two
fields needtoknow maintains, `needtoknow_terms` and `needtoknow_write`. `create_search_index`
creates the collection if needed and a `vectorSearch` index named `needtoknow`, with the vector
(cosine similarity by default) and, as filter fields, the permission terms, the write marker and
`_id`.

## Writes become searchable shortly after

Atlas Vector Search indexes a write a moment after MongoDB stores it, not at once. A retrieval made
in between would not see a new chunk, and, worse for permissions, could still match a replaced
chunk by its old permission terms. needtoknow's re-check reads the stored metadata, so such a
chunk is dropped rather than leaked, but `add` and `delete` close the gap by default:

- every `add` writes a fresh value of `needtoknow_write` on each chunk, and returns once an exact
  search filtered on that value finds every chunk it wrote. That is also when their new permission
  terms are searchable.
- every `delete` returns once the index no longer returns the deleted ids.

Both raise `TimeoutError` after `wait_timeout` seconds (60 by default). Bulk ingestion that does not
need to read its own writes can pass `wait=False`.

## Tuning

- **`num_candidates`** (default 100) is how many neighbours the approximate search considers. It
  is raised to `k` when `k` is larger, and Atlas caps it at 10,000. Higher finds more of the true
  nearest chunks and takes longer. MongoDB suggests about 20 times the number of results.
- **`exact=True`** compares the query with every chunk that passes the filter instead: exact
  results, at a higher cost on large collections.
- For a native `aretrieve`, pass `async_collection=` the same collection from an
  `AsyncMongoClient`; otherwise async calls run in a thread.

## How permissions are indexed

The backend uses the same design as [Elasticsearch, OpenSearch](elasticsearch.md#how-permissions-are-indexed)
and [Milvus](milvus.md#how-permissions-are-indexed): one array field holds a type-tagged term for
every string and integer in the chunk's metadata, so `1` and `"1"` stay apart and booleans, floats,
nested lists and objects never match. A filter becomes `$in` on that field, joined with `$and` and
`$or`. It is a BSON document, so a group name or user id is always a value and never query syntax.
An `In` with no values matches nothing without a call to MongoDB. Only one field needs indexing,
whatever your metadata looks like.

## Collections written by other tools

A document without `needtoknow_terms` is invisible to a pre-filtered search, though post-filtering
still finds it and checks its metadata. Fill the field in your ingestion pipeline:

```python
from needtoknow.backends.mongodb import permission_terms

document["needtoknow_terms"] = permission_terms(document["metadata"])
```

## Keyword search and hybrid search

`mode="keyword"` searches the chunks' text with Atlas Search (`$search`, BM25), under the same
permission filter. Atlas Search keeps text in an index of its own, which `create_text_index`
creates: the text with the `lucene.standard` analyzer, which keeps every word, and the permission
terms, the write marker and `_id` as tokens. One collection then serves both halves of
[hybrid search](../hybrid.md):

```python
from needtoknow import RankFusion
from needtoknow.backends.mongodb import create_text_index

create_search_index(collection, dim=768)
create_text_index(collection)  # the "needtoknow_text" index
vector = MongoDBBackend(collection, embed=embed, text_index="needtoknow_text")
keyword = MongoDBBackend(collection, mode="keyword")
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

Atlas Search also indexes writes a moment after they happen. A backend given `text_index` waits for
that index as well, so write through the vector backend above and both searches see each change
when `add`, `set_metadata` or `delete` returns. The filter is written with Atlas Search's own
operators (`in` on the permission terms, `compound` for `And` and `Or`), so values stay data, and
the query's text is the `text` operator's query, separate from the filter. Scores are Atlas
Search's BM25, higher for better matches. A keyword backend without `embed` writes chunks with no
vector, which keyword search finds and vector search does not. CI runs the conformance kit against
Atlas Local in keyword mode and fused with vector search.
