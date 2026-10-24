# Haystack

```bash
pip install "wardowl[haystack]"
```

`WardowlRetriever` is a Haystack component that returns only the documents a subject may read,
from any wardowl backend and authorizer. The extra requires `haystack-ai` 2.12 or later; CI tests
the current Haystack 3 release.

```python
from haystack import Pipeline

from wardowl import PermissionedRetriever
from wardowl.haystack import WardowlRetriever

pipeline = Pipeline()
pipeline.add_component("retriever", WardowlRetriever(PermissionedRetriever(backend, authorizer), top_k=5))
# ... a prompt builder and a generator, connected to "retriever.documents"

result = pipeline.run({"retriever": {"query": question, "subject": subject}})
```

## The subject is a run input

One pipeline serves every user: the application passes the authenticated user as `subject` on
each run, and the component refuses to run without a wardowl `Subject`. Build it from your
session, the user id and groups your identity provider vouches for, never from the request body or
from a model's output ([security model](../security.md#the-subject-must-come-from-authentication)).

## In an agent: `BoundWardowlRetriever`

An agent's model fills in the inputs of the tools it calls, and `ComponentTool` offers it every
input of the component, `subject` included: with `WardowlRetriever` as a tool, a prompt could
search as anyone. Agents, and any pipeline whose inputs a model sets, must use
`BoundWardowlRetriever`, which takes the subject from a function and has no `subject` input:

```python
import contextvars

from haystack.tools import ComponentTool

from wardowl.haystack import BoundWardowlRetriever

current_subject = contextvars.ContextVar("current_subject")  # your web layer sets it per request

search = ComponentTool(
    component=BoundWardowlRetriever(PermissionedRetriever(backend, authorizer), current_subject.get),
    name="search_documents",
    description="Search the company documents the current user may read.",
)
```

The function is called on every run, in the run's thread (or its task, for `run_async`), so a
contextvar the web layer set for the request is what it reads. A function that raises, as
`current_subject.get` does when no request set it, or that returns anything but a `Subject`, stops
the run with nothing returned.

## Inputs and outputs

| | Name | Meaning |
|---|---|---|
| Input | `query` | The question to search for |
| Input | `subject` | The authenticated user (`wardowl.Subject`); `WardowlRetriever` only |
| Input | `top_k` | Optional; overrides the component's `top_k` for this run |
| Input | `query_embedding` | Optional; a vector to search with instead of embedding `query` (connect a text embedder's `embedding`). `query` may then be empty. See [Your own vectors](../vectors.md) |
| Output | `documents` | Haystack `Document`s: the chunk id, text and score, best first, with the metadata keys named in `metadata_fields` |
| Output | `complete` | False when wardowl knows results may be missing (see [concepts](../concepts.md)) |

`run_async` has the same inputs and outputs, for `Pipeline.run_async`. Any failure of the
permission service raises `AuthorizationError`, so the pipeline stops rather than generating from
unfiltered documents.

## Metadata

Documents carry no metadata unless you name it, as in
`WardowlRetriever(retriever, metadata_fields=["title", "source"])`. Permission fields such as
`allowed_users`, `allowed_groups` and `tenant` say who may read what, and documents reach the model,
the prompt and any trace of them, so leave those out.

The components hold live connections to your vector store and permission service, so a pipeline
that contains one cannot be serialized to YAML; build the pipeline in code.
