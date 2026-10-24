# Your own vectors

A text query is embedded by the backend's `embed` function. When you already have the query's
vector, pass a `Query` instead of a string:

```python
from wardowl import Query

vector = embedding_service.embed(question)  # your model, service or cache
result = retriever.retrieve(Query(text=question, vector=vector), subject, k=5)
```

The vector is searched with as it is; the backend does not embed the text again. Permissions apply
exactly as for a text query: the same filter, the same re-check, the same `RetrievalResult`.

This covers:

- **Embedding outside the retriever**: a batching embedding service, a GPU worker, a cache of
  frequent questions, or a model the backend cannot call.
- **Other modalities**: an image, audio clip or document page embedded by a multimodal model, and
  searched against chunks embedded by the same model. `Query(vector=...)` needs no text.
- **Hybrid search with one embedding**: `RankFusion` gives every backend the same query, so vector
  backends use the vector and keyword backends use the text.
- **Backends without an embedding function**: if your ingestion pipeline computes the vectors,
  build the backend without `embed`, write with `add(documents, embeddings=...)` and search with
  `Query(vector=...)`.

## A store whose vectors you compute

```python
from wardowl import PermissionedRetriever, Query
from wardowl.backends.qdrant import QdrantBackend

backend = QdrantBackend(client, "pages")  # no embed function
backend.add(pages, embeddings=[image_model.embed(p.image) for p in pages])

retriever = PermissionedRetriever(backend, authorizer)
result = retriever.retrieve(Query(vector=image_model.embed(photo)), subject, k=10)
```

`image_model` stands for whatever multimodal model you use; wardowl only sees the numbers. The
query vector must come from the same model, and have the same dimensions, as the stored vectors.
On a vector backend built without `embed`, a text query raises `ValueError` rather than being
searched some other way (Chroma is the exception: it falls back to the collection's own embedding
function).

A query vector with the wrong number of dimensions makes the retrieval raise, and nothing is
returned. `FaissBackend` checks the length itself and raises `ValueError` before searching. Vespa
and Pinecone refuse the query, and their backends raise `RuntimeError` with the store's message.
Every other backend hands the vector to its store's client, and the store's own error comes back
unchanged (an exception from psycopg, redis-py, the Qdrant client and so on), since the retriever
does not catch errors from a backend. The same holds when `embed` returns vectors of another
model's size.

## Hybrid search with one embedding

```python
from wardowl import PermissionedRetriever, Query, RankFusion
from wardowl.backends.pgvector import PgVectorBackend

vector = PgVectorBackend(pool)  # vectors come with each query
keyword = PgVectorBackend(pool, mode="keyword")
retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)

result = retriever.retrieve(Query(text=question, vector=embed(question)), subject)
```

## What each backend uses

| Backend | Text query | `Query(vector=...)` | `Query(text=..., vector=...)` |
|---|---|---|---|
| pgvector, Qdrant, Milvus, MongoDB, Weaviate, Redis, Elasticsearch, OpenSearch, LanceDB, Vespa, FAISS, Cassandra, Pinecone | embedded with `embed` | the vector | the vector |
| The same in `mode="keyword"` (all but FAISS, Cassandra and Pinecone, which search vectors only) | the text | `ValueError` | the text |
| Chroma | `embed`, or the collection's embedding function | the vector | the vector |
| `InMemoryBackend` (word matching) | the text | `ValueError` | the text |
| `LangChainVectorStoreBackend` | `similarity_search` | `similarity_search_by_vector` | `similarity_search_by_vector` |
| `LlamaIndexBackend` | the index embeds it | a `QueryBundle` with the embedding | a `QueryBundle` with both |
| `RankFusion` | passed to every backend | passed to every backend | passed to every backend |

The framework integrations pass vectors on too: the Haystack component has a `query_embedding`
input for a text embedder's output, and the LlamaIndex retriever forwards a `QueryBundle`'s
embedding when one is set.

## Audit fingerprints

Events never contain the query's text or vector. With an `audit_key`, `query_hmac` fingerprints a
query by its text when it has text, so the same question matches whether or not a vector came
with it, and by its vector otherwise. Equal vectors give equal fingerprints; see
[Audit events](operations.md#audit-events).

## In your own backend

`search` receives the query exactly as the caller passed it: a string, or a `Query`. A backend that
takes only strings keeps working with string queries. To accept vectors:

```python
from wardowl import Query


class MyBackend:
    supports_filters = True

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        q = Query.of(query)  # a string becomes Query(text=...)
        vector = q.embedded(self.embed)  # the given vector, or embed(text)
        ...
```

`q.keywords()` gives the text for keyword search and raises `ValueError` for a vector-only query.
`check_backend(factory, query=Query(vector=...))` runs the [conformance kit](extending.md) with
vector queries.
