---
layout: landing
description: Permission-aware retrieval for RAG. Every user gets only the chunks they are allowed to read, whatever vector store, permission system or framework you use.
---

# Retrieval that knows who's asking

```{raw} html
<div class="ntk-hero">
  <p class="ntk-hero__lead">
    needtoknow puts your permission system inside RAG retrieval. Every user gets only the chunks
    they are allowed to read, whatever vector store, permission system or framework you use.
  </p>
  <div class="ntk-hero__actions">
    <a class="sd-btn sd-btn-primary sd-rounded-pill" href="quickstart.html">Get started</a>
    <a class="sd-btn sd-btn-outline-primary sd-rounded-pill" href="https://github.com/muzakkirhussain011/needtoknow">View on GitHub</a>
  </div>
</div>
```

```bash
pip install needtoknow
```

```{raw} html
<div class="ntk-hero-visual">
  <div class="ntk-hero__stage" data-ntk-hero>
    <img src="_static/brand/needtoknow-3d.webp" width="440" height="361" alt="The needtoknow logo: a document whose readable lines are highlighted and one line is redacted">
  </div>
</div>
<script type="module" src="_static/hero3d.js"></script>
```

## Built for production RAG

A RAG assistant over company documents is only as safe as its retrieval step. If the search ignores
who is asking, an engineer can ask about salaries and get HR documents summarised back. needtoknow
sits between your application and your vector store, and closes that gap.

::::{grid} 1 2 3 3
:gutter: 3

:::{grid-item-card} {octicon}`stack;1.4em;sd-text-primary` Works with what you have
pgvector, Qdrant, Milvus, MongoDB, Weaviate, Redis, Elasticsearch, OpenSearch, Chroma or any
LangChain or LlamaIndex store; OpenFGA, SpiceDB, Cerbos, Open Policy Agent or your own rules. The
core has no dependencies.
:::

:::{grid-item-card} {octicon}`goal;1.4em;sd-text-primary` Exact where it matters
With the filter inside the search, results are the true top-k of what the user may read, not the
top-k of everything with the forbidden ones removed.
:::

:::{grid-item-card} {octicon}`shield-lock;1.4em;sd-text-primary` Fails closed
Errors, timeouts and malformed answers from the permission system raise an error. They never
return unfiltered results.
:::

:::{grid-item-card} {octicon}`shield-check;1.4em;sd-text-primary` Defence in depth
Pre-filtered results are checked again, so a backend that ignores the filter cannot leak, and each
result counts the violations it caught.
:::

:::{grid-item-card} {octicon}`pulse;1.4em;sd-text-primary` Observable and auditable
Every retrieval emits an audit event and, optionally, an OpenTelemetry span, without the query or
chunk text.
:::

:::{grid-item-card} {octicon}`zap;1.4em;sd-text-primary` Sync and async
`retrieve` and `aretrieve` share one implementation of the security logic, with native async
clients where the store has them.
:::
::::

## How it works

::::{grid} 1 1 3 3
:gutter: 3

:::{grid-item-card} 1. Ask the permission system
:class-card: ntk-step
The authorizer turns the user into a filter (OpenFGA, SpiceDB, Cerbos, Open Policy Agent, or
permissions stored on the chunks), or checks candidates one by one.
:::

:::{grid-item-card} 2. Filter inside the search
:class-card: ntk-step
The filter runs inside the vector store's own query, so the nearest chunks the user may read come
back, however few of the corpus that is.
:::

:::{grid-item-card} 3. Check, then return
:class-card: ntk-step
Every hit is checked again before it reaches the prompt. Anything that cannot be decided raises an
error instead.
:::
::::

## Your stack, unchanged

:::::{tab-set}

::::{tab-item} Permissions on the chunks

```python
from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]}),
    ]
)
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())

result = retriever.retrieve("leave salary bands", Subject("bob", groups=["engineering"]), k=3)
print([hit.id for hit in result.hits])  # ['handbook']
```

::::

::::{tab-item} pgvector + OpenFGA

```python
from psycopg_pool import ConnectionPool

from needtoknow import PermissionedRetriever, Subject
from needtoknow.authorizers.openfga import OpenFGAAuthorizer
from needtoknow.backends.pgvector import PgVectorBackend

backend = PgVectorBackend(ConnectionPool("postgresql://localhost/app"), embed=embed)
authorizer = OpenFGAAuthorizer("http://localhost:8080", store_id, resource_field="doc_id")
retriever = PermissionedRetriever(backend, authorizer)

result = retriever.retrieve("What are the salary bands?", Subject("alice"))
```

::::

::::{tab-item} LangChain

```python
from needtoknow import MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.langchain import NeedToKnowRetriever

retriever = NeedToKnowRetriever(retriever=PermissionedRetriever(backend, MetadataACLAuthorizer()), k=4)

docs = retriever.invoke("What changed in Q3 pricing?", subject=Subject("bob", ["sales"]))
```

::::

::::{tab-item} LlamaIndex

```python
from llama_index.core.query_engine import RetrieverQueryEngine

from needtoknow import MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.llamaindex import NeedToKnowRetriever

permissioned = PermissionedRetriever(backend, MetadataACLAuthorizer())
retriever = NeedToKnowRetriever(permissioned, Subject("bob", ["engineering"]), k=4)

engine = RetrieverQueryEngine.from_args(retriever)  # uses your configured LLM
```

::::

:::::

## Integrations

::::{grid} 1 1 3 3
:gutter: 3

:::{grid-item-card} {octicon}`database;1.2em;sd-text-primary` Vector stores
- [PostgreSQL + pgvector](integrations/pgvector.md)
- [Qdrant](integrations/qdrant.md)
- [Milvus](integrations/milvus.md)
- [MongoDB Atlas Vector Search](integrations/mongodb.md)
- [Weaviate](integrations/weaviate.md)
- [Redis](integrations/redis.md)
- [Elasticsearch and OpenSearch](integrations/elasticsearch.md)
- [Chroma](integrations/chroma.md)
- Any [LangChain](integrations/langchain.md) or [LlamaIndex](integrations/llamaindex.md) store
:::

:::{grid-item-card} {octicon}`key;1.2em;sd-text-primary` Permission systems
- [OpenFGA](integrations/openfga.md)
- [SpiceDB](integrations/spicedb.md)
- [Cerbos](integrations/cerbos.md)
- [Open Policy Agent](integrations/opa.md)
- Permissions on the chunks, or [your own rules](extending.md)
:::

:::{grid-item-card} {octicon}`plug;1.2em;sd-text-primary` Frameworks and operations
- [LangChain](integrations/langchain.md) retrievers and agent tools
- [LlamaIndex](integrations/llamaindex.md) retrievers and postprocessors
- [Haystack](integrations/haystack.md) retriever component
- [OpenTelemetry](operations.md#tracing) spans
- [Audit events](operations.md) as JSON lines
:::
::::

## Measured, not claimed

::::{grid} 2 2 4 4
:gutter: 3

:::{grid-item-card}
:class-card: ntk-stat
<span class="ntk-stat__value">0</span>
forbidden chunks returned in 21,000 benchmark retrievals
:::

:::{grid-item-card}
:class-card: ntk-stat
<span class="ntk-stat__value">0.88–0.97</span>
recall@10 at a million chunks, pre-filtered on pgvector
:::

:::{grid-item-card}
:class-card: ntk-stat
<span class="ntk-stat__value">129/s</span>
retrievals on 4 CPUs, a million chunks in PostgreSQL
:::

:::{grid-item-card}
:class-card: ntk-stat
<span class="ntk-stat__value">8–10%</span>
cost of a remote permission service, all of it recovered by caching
:::
::::

Every number comes from a reproducible run, with the method and full tables on the
[Benchmarks](benchmarks.md) page.

## Secure by default

::::{grid} 1 1 3 3
:gutter: 3

:::{grid-item-card} {octicon}`circle-slash;1.2em;sd-text-primary` No silent fallback
An unavailable or misbehaving permission system stops retrieval; it never widens it. The
[security model](security.md) states what is guaranteed and what is assumed.
:::

:::{grid-item-card} {octicon}`verified;1.2em;sd-text-primary` Verifiable releases
Every release carries Sigstore signatures, a CycloneDX SBOM and PyPI attestations, published by
GitHub Actions without stored credentials.
:::

:::{grid-item-card} {octicon}`lock;1.2em;sd-text-primary` Hardened supply chain
Actions pinned to commits, CodeQL on the code and the workflows, and a published OpenSSF
Scorecard.
:::
::::

```{raw} html
<div class="ntk-cta">
  <a class="sd-btn sd-btn-primary sd-rounded-pill" href="quickstart.html">Get started</a>
  <a class="sd-btn sd-btn-outline-primary sd-rounded-pill" href="concepts.html">Read the concepts</a>
</div>
```

```{toctree}
:hidden:
:caption: Guide

quickstart
concepts
combining
hybrid
vectors
security
operations
production
```

```{toctree}
:hidden:
:caption: Integrations

integrations/index
integrations/pgvector
integrations/chroma
integrations/elasticsearch
integrations/qdrant
integrations/milvus
integrations/mongodb
integrations/weaviate
integrations/redis
integrations/opa
integrations/openfga
integrations/spicedb
integrations/cerbos
integrations/langchain
integrations/llamaindex
integrations/haystack
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
