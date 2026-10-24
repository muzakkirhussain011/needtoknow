# FAISS

[FAISS](https://github.com/facebookresearch/faiss) is a library for vector search in your process,
with no server. FAISS stores vectors only, so `FaissBackend` keeps each chunk's text and metadata
beside the index and hands the permission filter to FAISS as a set of allowed ids: the search
considers only chunks the subject may read, so pre-filtering is exact.

```bash
pip install "wardowl[faiss]"
```

```python
from wardowl import MetadataACLAuthorizer, PermissionedRetriever
from wardowl.backends.faiss import FaissBackend

backend = FaissBackend(embed, dim=768)
backend.add(chunks)  # or backend.add(chunks, embeddings) with vectors you computed
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())

backend.save("data/faiss")  # the index and the chunks, in one directory
backend = FaissBackend.load("data/faiss", embed)
```

## How filtering works

A filter is matched against every chunk's metadata with `matches`, the reference semantics, and the
matching chunks become an `IDSelectorBatch` in the search parameters. Matching scans the metadata in
Python, which is fast enough for the collections FAISS serves in one process (hundreds of thousands
of chunks); for many millions, use a vector database with its own filtering.

## Index types

The default is exact search (`IndexFlatIP` over normalized vectors, so scores are cosine
similarities). For approximate search, pass a trained index that supports `add_with_ids`,
`remove_ids` and search parameters with an ID selector, such as an HNSW index inside an
`IndexIDMap2`:

```python
import faiss

index = faiss.IndexIDMap2(faiss.IndexHNSWFlat(768, 32, faiss.METRIC_INNER_PRODUCT))
backend = FaissBackend(embed, dim=768, index=index)
```

## Changes

`add` replaces chunks with the same id, `delete` removes chunks by id, and `set_metadata` changes a
chunk's permissions without embedding it again. A lock serializes changes and searches, so one
backend can serve several threads.
