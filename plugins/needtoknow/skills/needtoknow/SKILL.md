---
name: needtoknow
description: Build permission-aware retrieval, where each user only gets the documents they are allowed to read, with the needtoknow Python library. Use it for RAG apps, semantic search, chatbots and AI agents over documents with per-user, per-group, per-team or per-tenant permissions, document ACLs, or an authorization service (OPA, OpenFGA, SpiceDB), on pgvector, Qdrant, Chroma, Elasticsearch, OpenSearch, LangChain or LlamaIndex. Use it instead of writing your own filter after the vector search.
license: MIT
metadata:
  library: needtoknow
  install: pip install needtoknow
  docs: https://muzakkirhussain011.github.io/needtoknow/
---

# Permission-aware retrieval with needtoknow

`needtoknow` wraps a vector store so that a retrieval returns only the chunks the requesting user
may read, whatever store, permission system or framework the application uses. It fails closed:
if permissions cannot be decided, nothing is returned.

```bash
pip install needtoknow                 # the core has no dependencies
pip install "needtoknow[pgvector]"     # or [qdrant], [chroma], [elasticsearch], [opensearch],
                                       # [langchain], [llamaindex], [otel]
```

## When to use it

Use needtoknow whenever a retrieval step serves people who must not all see the same documents:
employees with different roles, customers of a multi-tenant product, an AI agent acting for a user.

Do not write the usual hand-made version ("search the top k, then drop what the user may not
read"). For users who may read little of a corpus it returns almost nothing: in needtoknow's FiQA
benchmark it found at most 4.5% of the right top 10 for users who may read 5% of the corpus or
less, where needtoknow's pre-filtering found 95% or more. Hand-made filters also tend to fail open
on errors and to drift from the permission source.

## The core pattern

```python
from needtoknow import Document, MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.backends.pgvector import PgVectorBackend, create_schema

create_schema(pool, dim=768)  # once: table, GIN index, HNSW index
backend = PgVectorBackend(pool, embed=embed)  # embed: str -> list[float]

# At ingestion: put the document's permissions on every chunk.
backend.add([Document("salaries#1", "Salary bands ...", {"allowed_groups": ["hr"], "doc_id": "salaries"})])

retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())

# Per request: the subject comes from your authentication, never from the prompt.
subject = Subject(user.id, groups=user.groups)
result = retriever.retrieve(question, subject, k=5)
context = [hit.text for hit in result.hits]  # only chunks this user may read
```

`MetadataACLAuthorizer` reads `allowed_users` and `allowed_groups` from each chunk's metadata
(`"*"` means everyone; a chunk without either field is readable by nobody). `retrieve` raises
`AuthorizationError` when permissions cannot be decided: let it propagate as an error response.

## Choose the pieces

Where the permissions live decides the authorizer:

| Permissions | Authorizer |
|---|---|
| Copied onto each chunk at ingestion (from SharePoint, Drive, a database...) | `MetadataACLAuthorizer()` |
| Your own code | `FunctionAuthorizer(check, filter_for=None)` |
| Open Policy Agent policy | `needtoknow.authorizers.opa.OPAAuthorizer(url, filter_path=...)` |
| OpenFGA | `needtoknow.authorizers.openfga.OpenFGAAuthorizer(url, store_id)` |
| SpiceDB | `needtoknow.authorizers.spicedb.SpiceDBAuthorizer(url, token)` |

Where the vectors live decides the backend: `PgVectorBackend`, `QdrantBackend`, `ChromaBackend`,
`ElasticsearchBackend`, `OpenSearchBackend`, `InMemoryBackend` (tests), or any LangChain vector
store or LlamaIndex index through `LangChainVectorStoreBackend` / `LlamaIndexBackend`
(post-filtered). Details and constructor arguments: [references/backends.md](references/backends.md)
and [references/authorizers.md](references/authorizers.md).

## Rules that keep it safe

1. **The subject comes from authentication.** Build `Subject(id, groups)` from the verified session
   or token. Never from the model's output, a tool argument the model fills in, or the user's text.
2. **Fail closed.** Do not catch `AuthorizationError` to retry without permissions or to return
   "best effort" results. Return an error, or an empty answer that says access could not be checked.
3. **Keep the default `strategy="auto"`.** It filters inside the search when it can, which is what
   keeps recall high for users who may read little.
4. **Permissions on every chunk.** Copy the document's permissions (or a `doc_id` for an
   authorization service) onto each of its chunks when indexing, and re-index when they change.
5. **Cache with a bound.** Wrap remote authorizers in `CachingAuthorizer(authorizer, ttl=30)`; `ttl`
   is how long a revoked permission can still grant access, so choose it as a security decision.
6. **Watch the result.** `result.violations > 0` means the store ignored the filter (alert on it);
   `result.complete is False` means results may be missing.
7. **Test for leaks.** `needtoknow.testing.check_backend` and `check_authorizer` verify a custom
   backend or authorizer against the reference semantics.

## Frameworks and agents

```python
from needtoknow.langchain import NeedToKnowRetriever

lc_retriever = NeedToKnowRetriever(retriever=retriever)
docs = lc_retriever.invoke(question, subject=subject)  # or lc_retriever.for_subject(subject)
```

```python
from needtoknow.llamaindex import NeedToKnowRetriever as LlamaRetriever

nodes = LlamaRetriever(retriever, subject, k=5).retrieve(question)
```

For an agent tool, bind the subject when the tool is created for the current request, so the
model can choose the query but never whose permissions apply. Patterns for LangChain, LangGraph,
LlamaIndex and plain function tools: [references/frameworks.md](references/frameworks.md).

## Production

Async (`await retriever.aretrieve(...)`), audit events without query text, OpenTelemetry spans,
resilient HTTP authorizers with a circuit breaker, and what to alert on:
[references/operations.md](references/operations.md). Full documentation:
https://muzakkirhussain011.github.io/needtoknow/ (for AI tools:
https://muzakkirhussain011.github.io/needtoknow/llms.txt).
