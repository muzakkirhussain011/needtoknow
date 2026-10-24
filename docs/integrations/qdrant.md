# Qdrant

```bash
pip install "wardowl[qdrant]"
```

The permission filter becomes Qdrant's `query_filter`, so Qdrant returns the nearest points the
subject may read. Qdrant applies filters inside its HNSW search and plans restrictive filters with
its payload indexes, which suits large collections where a user may read only a small share.

## Set up

```python
from qdrant_client import QdrantClient

from wardowl.backends.qdrant import QdrantBackend, create_collection

client = QdrantClient(url="http://localhost:6333")  # or QdrantClient(":memory:") for tests
create_collection(client, "docs", dim=768)
backend = QdrantBackend(client, "docs", embed=embed)  # embed: str -> list[float], your embedding model
```

`create_collection(client, name, dim, keyword_fields=("allowed_users", "allowed_groups"))` creates
a cosine-distance collection if it does not exist, and keyword payload indexes on the permission
fields. Without the indexes Qdrant still filters correctly, but has to check every point.

## How points are stored

`QdrantBackend` stores and reads points in the layout of LangChain's Qdrant integration: the text
under `page_content`, the metadata under `metadata`. A collection built with LangChain can therefore
be searched with permissions without re-indexing, as long as its metadata carries the permission
fields. Pass `content_key=` and `metadata_key=` for another layout, and `vector_name=` for a named
vector.

Qdrant point ids are unsigned integers or UUIDs. `add` uses a document id that is a UUID as the point
id, and derives a stable UUID from any other id, keeping the original under `wardowl_id` in the
payload. Search results report that id, or the point id for points written by something else. A
point without a `metadata` object carries no permissions, so the re-check denies it.

```python
from wardowl import Document

backend.add([Document("salaries-1", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]})])
backend.delete(["salaries-1"])
```

## How filters are translated

| wardowl | Qdrant |
|---|---|
| `In("allowed_groups", ["hr", "eng"])` | `FieldCondition(key="metadata.allowed_groups", match=MatchAny(any=["eng", "hr"]))` |
| `In("level", ["a", 1])` | one `MatchAny` for the strings and one for the integers, under `should` |
| `And(...)` / `Or(...)` | `Filter(must=[...])` / `Filter(should=[...])` |
| `In(field, [])` | matches nothing: the backend returns no hits without calling Qdrant |

For a list field, Qdrant matches when any element matches, which is the reference semantics of `In`.
Strings and integers are never mixed in one `MatchAny`, and an empty value list is handled before the
query is built rather than left to Qdrant's handling of empty lists. Field names used in filters
may not contain `.`, `[`, `]` or `"`, which Qdrant reads as path syntax; such a filter raises
`ValueError`, so retrieval fails closed.

The backend passes wardowl's [conformance kit](../extending.md#test-it-with-the-conformance-kit)
in CI against a real Qdrant server (v1.19.2) and against Qdrant's local mode.

## Search quality and async

`search_params=` takes a `qdrant_client.models.SearchParams`: `SearchParams(hnsw_ef=128)` searches
deeper for higher recall, `SearchParams(exact=True)` searches by brute force. Pass
`async_client=AsyncQdrantClient(...)` for a native `aretrieve`; without it, async calls run the sync
search in a thread.

## Keyword search and hybrid search

`mode="keyword"` ranks the chunks by BM25, under the same `query_filter`, with no model to run.
`create_collection(..., text_search=True)` adds a sparse vector, `wardowl_bm25`, with Qdrant's
IDF modifier. The backend writes each chunk's BM25 term weights there, and Qdrant multiplies in the
inverse document frequency of each word at search time, so the score is BM25 and it stays right as
the collection grows. One collection then serves both halves of [hybrid search](../hybrid.md):

```python
from wardowl import RankFusion

create_collection(client, "docs", dim=768, text_search=True)
vector = QdrantBackend(client, "docs", embed=embed, text_search=True)  # writes both vectors
keyword = QdrantBackend(client, "docs", mode="keyword")
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
```

Words are runs of letters, digits and underscores, lowercased, and every word is kept: there is no
stemming and no stop-word list. Each word's index in the sparse vector is its CRC-32, and its weight
is BM25's term-frequency part with k1 = 1.2, b = 0.75 and Qdrant's average length of 256 words.
`bm25_terms(text)` computes the same weights, for points written by other tools. Write chunks
through a backend with `text_search=True` (or `mode="keyword"`), or keyword search will not find
them. CI runs the conformance kit in keyword mode and fused with vector search, in Qdrant's local
mode and against Qdrant 1.19.2.
