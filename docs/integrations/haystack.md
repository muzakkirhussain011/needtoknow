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
each run, as a wardowl `Subject` or a function that returns one, and the component refuses to run
with anything else. Build it from your session, the user id and groups your identity provider
vouches for, never from the request body or from a model's output
([security model](../security.md#the-subject-must-come-from-authentication)).

The `subject` input accepts any type, so that a value a model sends for it stays plain JSON (see
below). A component output typed `Subject` connects to it. Name the sockets when you connect to the
component, as in `pipeline.connect("embedder.embedding", "retriever.query_embedding")`: a
connection by component name alone is ambiguous.

## In an agent

An agent's model fills in the inputs of the tools it calls, so it must never be able to fill in
`subject`. `ComponentTool` leaves `WardowlRetriever`'s `subject` out of the tool's parameters,
because the input's type includes a function. Haystack still passes on any argument a model sends,
even one the tool does not offer, and in an agent a model's value wins over the state's. Such a
value reaches the component as the JSON it is, never as a `Subject`, so the call fails with nothing
retrieved.

Give the tool the subject from the agent's state:

```python
from haystack.components.agents import Agent
from haystack.tools import ComponentTool

from wardowl import Subject
from wardowl.haystack import WardowlRetriever

search = ComponentTool(
    component=WardowlRetriever(PermissionedRetriever(backend, authorizer)),
    name="search_documents",
    description="Search the company documents the current user may read.",
    inputs_from_state={"subject": "subject"},
)
agent = Agent(chat_generator=chat_generator, tools=[search], state_schema={"subject": {"type": Subject}})

result = agent.run(messages=messages, subject=subject)  # the authenticated user, from your session
```

### `BoundWardowlRetriever`

`BoundWardowlRetriever` takes the subject from a function instead and has no `subject` input at
all, for when your web layer sets the user in a contextvar for each request:

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
| Input | `subject` | The authenticated user: a `wardowl.Subject`, or a function returning one; `WardowlRetriever` only |
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
