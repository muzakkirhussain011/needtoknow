# Haystack

```bash
pip install "needtoknow[haystack]"
```

`NeedToKnowRetriever` is a Haystack component that returns only the documents a subject may read,
from any needtoknow backend and authorizer. The extra requires `haystack-ai` 2.12 or later; CI tests
the current Haystack 3 release.

```python
from haystack import Pipeline

from needtoknow import PermissionedRetriever
from needtoknow.haystack import NeedToKnowRetriever

pipeline = Pipeline()
pipeline.add_component("retriever", NeedToKnowRetriever(PermissionedRetriever(backend, authorizer), top_k=5))
# ... a prompt builder and a generator, connected to "retriever.documents"

result = pipeline.run({"retriever": {"query": question, "subject": subject}})
```

## The subject is a run input

One pipeline serves every user: the application passes the authenticated user as `subject` on
each run, and the component refuses to run without a needtoknow `Subject`. Build it from your
session, the user id and groups your identity provider vouches for, never from the request body or
from a model's output ([security model](../security.md#the-subject-must-come-from-authentication)).
In an agent, keep `subject` out of the tool's inputs that the model fills in.

## Inputs and outputs

| | Name | Meaning |
|---|---|---|
| Input | `query` | The question to search for |
| Input | `subject` | The authenticated user (`needtoknow.Subject`) |
| Input | `top_k` | Optional; overrides the component's `top_k` for this run |
| Input | `query_embedding` | Optional; a vector to search with instead of embedding `query` (connect a text embedder's `embedding`). `query` may then be empty. See [Your own vectors](../vectors.md) |
| Output | `documents` | Haystack `Document`s: the chunk id, text, metadata and score, best first |
| Output | `complete` | False when needtoknow knows results may be missing (see [concepts](../concepts.md)) |

`run_async` has the same inputs and outputs, for `Pipeline.run_async`. Any failure of the
permission service raises `AuthorizationError`, so the pipeline stops rather than generating from
unfiltered documents.

The component holds live connections to your vector store and permission service, so a pipeline
that contains it cannot be serialized to YAML; build the pipeline in code.
