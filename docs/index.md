# needtoknow

**Permission-aware retrieval for RAG.** Every user gets only the chunks they are allowed to read,
whatever vector store, permission system or framework you use.

```python
from needtoknow import MetadataACLAuthorizer, PermissionedRetriever, Subject

retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
result = retriever.retrieve("What are this year's salary bands?", Subject("bob", groups=["engineering"]), k=5)
# result.hits holds only chunks Bob may read. HR documents never reach the prompt.
```

A RAG assistant over company documents is only as safe as its retrieval step. If the search
ignores who is asking, an engineer can ask about salaries and get HR documents summarised back.
needtoknow sits between your application and your vector store. It asks your permission system
what the user may read, puts that inside the search when it can, checks candidates after the
search when it cannot, and fails closed when anything goes wrong.

- **Works with what you have.** pgvector, Chroma, LangChain and LlamaIndex stores; Open Policy
  Agent, OpenFGA, permissions stored on the chunks, or your own function. The core has no
  dependencies.
- **Exact where it matters.** With a filter in the search, results are the true top-k of what the
  user may read, not the top-k of everything with the forbidden ones removed.
- **Fails closed.** Errors, timeouts and malformed answers from the permission system raise an
  error. They never return unfiltered results.
- **Defence in depth.** Pre-filtered results are checked again, so a backend that ignores the
  filter cannot leak, and each result reports how many such violations it caught.
- **Observable.** Every retrieval emits an audit event and, optionally, an OpenTelemetry span,
  without query or chunk text.

## Where to start

[Getting started](quickstart.md)
: Install, run a first permissioned search, and connect a real vector store.

[Concepts](concepts.md)
: Subjects, authorizers, filters, the two strategies and what each result tells you.

[Security model](security.md)
: What needtoknow guarantees, what it assumes, and what it leaves to you.

[Integrations](integrations/index.md)
: pgvector, Chroma, Open Policy Agent, OpenFGA, LangChain and LlamaIndex.

[Benchmarks](benchmarks.md)
: Recall, completeness and latency with real embeddings, across permission selectivity.

```{toctree}
:hidden:
:caption: Guide

quickstart
concepts
security
operations
```

```{toctree}
:hidden:
:caption: Integrations

integrations/index
integrations/pgvector
integrations/chroma
integrations/opa
integrations/openfga
integrations/langchain
integrations/llamaindex
```

```{toctree}
:hidden:
:caption: Reference

api
extending
benchmarks
changelog
contributing
```
