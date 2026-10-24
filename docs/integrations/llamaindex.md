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
score and the metadata named in `metadata_fields`, none by default:
`WardowlRetriever(permissioned, subject, metadata_fields=["title", "source"])`. Permission fields
such as `allowed_users`, `allowed_groups` and `tenant` say who may read what, and nodes reach the
model, prompts and traces, so leave those out. `aretrieve` uses wardowl's async path.

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

The authorizer sees each node's full metadata, but the nodes the postprocessor keeps carry only the
metadata named in `metadata_fields`, none by default, in their relationships too:
`PermissionNodePostprocessor(authorizer=authorizer, subject=subject, metadata_fields=["title"])`.
LlamaIndex puts a node's metadata into the prompt by default, so permission fields would otherwise
reach the model and traces. The kept nodes are copies; the nodes passed in are not changed.

## Any LlamaIndex index as a backend

```python
from wardowl.llamaindex import LlamaIndexBackend

backend = LlamaIndexBackend(index)  # anything with as_retriever(similarity_top_k=...)
retriever = PermissionedRetriever(backend, authorizer)  # post-filtered
```
