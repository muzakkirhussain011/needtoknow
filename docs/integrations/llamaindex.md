# LlamaIndex

```bash
pip install "wardowl[llamaindex]"
```

## A permission-aware retriever for query and chat engines

`WardowlRetriever` is a LlamaIndex retriever for one subject:

```python
from llama_index.core.query_engine import RetrieverQueryEngine

from wardowl import MetadataACLAuthorizer, PermissionedRetriever, Subject
from wardowl.llamaindex import WardowlRetriever

permissioned = PermissionedRetriever(backend, MetadataACLAuthorizer())
retriever = WardowlRetriever(permissioned, Subject("bob", ["engineering"]), k=4)

nodes = retriever.retrieve("How long is annual leave?")
engine = RetrieverQueryEngine.from_args(retriever)  # uses your configured LLM
```

Create one per request or session, from the authenticated user. Returned nodes carry the chunk's
metadata and score; `aretrieve` uses wardowl's async path.

## Adding permissions to an existing pipeline

`PermissionNodePostprocessor` drops the nodes a subject may not read, after whatever retrieval your
pipeline already does:

```python
from wardowl.llamaindex import PermissionNodePostprocessor

postprocessor = PermissionNodePostprocessor(authorizer=authorizer, subject=subject)
engine = index.as_query_engine(node_postprocessors=[postprocessor])
```

It post-filters, so it can return fewer nodes than were retrieved; `WardowlRetriever` with a
pre-filtering backend does not have that problem. Like the retriever, it fails closed: an
authorizer error, a missing decision or a non-boolean decision raises `AuthorizationError`.

## Any LlamaIndex index as a backend

```python
from wardowl.llamaindex import LlamaIndexBackend

backend = LlamaIndexBackend(index)  # anything with as_retriever(similarity_top_k=...)
retriever = PermissionedRetriever(backend, authorizer)  # post-filtered
```
