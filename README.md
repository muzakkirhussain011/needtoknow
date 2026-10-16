# needtoknow

[![CI](https://github.com/muzakkirhussain011/needtoknow/actions/workflows/ci.yml/badge.svg)](https://github.com/muzakkirhussain011/needtoknow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/core%20dependencies-none-brightgreen.svg)

**Permission-aware retrieval for RAG.** Every user gets only the chunks they are allowed to read,
whatever vector store, permission system or framework you use.

```python
from needtoknow import PermissionedRetriever, MetadataACLAuthorizer, Subject

retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
result = retriever.retrieve("What are this year's salary bands?", Subject("bob", groups=["engineering"]), k=5)
# result.hits holds only chunks Bob may read. HR documents never reach the prompt.
```

## Why

A RAG assistant over company documents is only as safe as its retrieval step. If the vector search
ignores who is asking, an engineer can ask the assistant about salaries and get HR documents
summarised back. Every team building internal RAG has to solve this, and most solve it by hand,
differently each time.

`needtoknow` is that layer as a small, tested library:

- **Works with what you have.** Wrap any search backend and any permission system. The core has
  no dependencies.
- **Filters in the right place.** When the permission system can describe "everything Bob may
  read" as a filter, the filter goes *inside* the vector search, so results are the true top-k of
  what Bob may read. When it cannot, candidates are checked after the search and the search widens
  until enough permitted results are found.
- **Fails closed.** If the permission service errors, times out or returns malformed answers, the
  user gets an error, never unfiltered results.
- **Defence in depth.** Pre-filtered results are checked again, so a backend bug that ignores the
  filter cannot leak. Each result reports how many such violations it caught.

## Install

```bash
pip install needtoknow
```

Python 3.10 or newer. Adapters for external systems install as optional extras.

## Quickstart

```python
from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]}),
        Document("runbook", "Roll back a failed deploy ...", {"allowed_groups": ["engineering"]}),
    ]
)
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())

result = retriever.retrieve("leave salary bands", Subject("bob", groups=["engineering"]), k=3)
print([hit.id for hit in result.hits])  # ['handbook']
print(result.strategy, result.complete)  # prefilter True
```

`aretrieve` is the async equivalent, with the same arguments and guarantees.

## How it works

| Strategy | When | Cost | Result |
|---|---|---|---|
| `prefilter` | The authorizer can return a filter and the backend supports filters | One search | The exact top-k of what the subject may read |
| `postfilter` | Any authorizer | Searches `k × overfetch` candidates, widening up to `max_candidates` | Exact when enough permitted candidates exist; `complete=False` otherwise |
| `auto` (default) | Picks `prefilter` when both sides support it | | |

Every call returns a `RetrievalResult` with the hits plus `strategy`, `examined`, `denied`,
`violations` (pre-filtered hits the re-check rejected, which should always be 0) and `complete`.

## Integrations

Each integration is an optional extra, so the core stays dependency-free.

**PostgreSQL + pgvector** (`pip install "needtoknow[pgvector]"`, pgvector 0.8+). The permission
filter runs inside the SQL query as JSON containment tests, served by a GIN index, and iterative
index scans keep HNSW returning k rows under selective filters.

```python
from psycopg_pool import ConnectionPool

from needtoknow.backends.pgvector import PgVectorBackend, create_schema

pool = ConnectionPool("postgresql://localhost/app")
create_schema(pool, dim=768)
backend = PgVectorBackend(pool, embed=my_embedding_model)  # embed: str -> list[float]
backend.add(documents)
```

**Chroma** (`pip install "needtoknow[chroma]"`). The filter becomes Chroma's `where` clause. Each
value in a permission list is stored as its own field rather than as a Chroma array, and an exact
copy of the metadata is kept for the re-check: in chromadb 1.5.x, deleting a collection leaves its
array metadata behind for new records in other collections to pick up
([chroma-core/chroma#7594](https://github.com/chroma-core/chroma/issues/7594)), which for
permissions would be a leak.

```python
import chromadb

from needtoknow.backends.chroma import ChromaBackend

collection = chromadb.PersistentClient(path="./chroma").get_or_create_collection("docs")
backend = ChromaBackend(collection)  # uses the collection's embedding function
```

**Open Policy Agent** (no extra dependency). Your Rego policy returns one decision per chunk, and
can optionally return a filter so the vector store does the filtering. A complete policy is in
[`examples/opa/needtoknow.rego`](examples/opa/needtoknow.rego).

```python
from needtoknow.authorizers.opa import OPAAuthorizer

authorizer = OPAAuthorizer("http://localhost:8181", filter_path="needtoknow/filter")
```

**OpenFGA** (no extra dependency). Relationship-based access control in the Google Zanzibar style.
Checks run in batches; for pre-filtering, ListObjects returns every document the user may view,
and if that list may have been truncated the retriever post-filters instead. The subject's groups
can be sent as contextual tuples, so memberships from your identity provider need no syncing.

```python
from needtoknow.authorizers.openfga import OpenFGAAuthorizer

authorizer = OpenFGAAuthorizer("http://localhost:8080", store_id, resource_field="doc_id")
```

**LangChain** (`pip install "needtoknow[langchain]"`). `NeedToKnowRetriever` is a LangChain
retriever, so it works in chains, agents and LangGraph tools; `LangChainVectorStoreBackend` turns
any LangChain vector store into a needtoknow backend (post-filtered).

```python
from needtoknow.langchain import NeedToKnowRetriever

retriever = NeedToKnowRetriever(retriever=PermissionedRetriever(backend, authorizer))
docs = retriever.invoke("What are this year's salary bands?", subject=Subject("bob", ["sales"]))
```

**LlamaIndex** (`pip install "needtoknow[llamaindex]"`). `NeedToKnowRetriever` for query and chat
engines, `PermissionNodePostprocessor` for existing pipelines, and `LlamaIndexBackend` to wrap an
existing index. Runnable examples for both frameworks are in [`examples/`](examples).

Filter values are strings or integers, the types permission identifiers use. That keeps every
backend's answer identical to the reference semantics, which the test suite checks for each one.

## Caching, audit and tracing

```python
from needtoknow import CachingAuthorizer, JsonLinesAudit
from needtoknow.otel import OpenTelemetryObserver  # pip install "needtoknow[otel]"

retriever = PermissionedRetriever(
    backend,
    CachingAuthorizer(authorizer, ttl=30),  # a revoked permission stops granting access within 30 s
    observers=[JsonLinesAudit(audit_file), OpenTelemetryObserver()],  # audit_file: an open text file
    audit_key=b"...",  # optional: an HMAC of each query, so repeats can be matched without storing them
)
```

`CachingAuthorizer` caches pre-filters and per-chunk decisions per subject for `ttl` seconds and
never caches an error; `invalidate()` drops decisions after a change you know about. Every
retrieval, including failed ones, produces one `RetrievalEvent` (who asked, strategy, returned ids,
denied and violation counts, duration, error type). Events never contain query or chunk text, and
an observer that fails is logged without affecting retrieval.

## Bring your own backend or permission system

A backend is any object with a `search` method:

```python
class MyBackend:
    supports_filters = True  # set False if search() ignores `filter`

    def search(self, query: str, k: int, filter=None) -> list[Hit]:
        """Return hits best first. Translate `filter` (In / And / Or) into your store's filter."""
```

An authorizer answers two questions:

```python
class MyAuthorizer:
    def check(self, subject, hits, action="read") -> list[bool]:
        """One decision per hit, in order. Raise on any doubt: the retriever fails closed."""

    def filter_for(self, subject, action="read"):
        """A Filter selecting everything the subject may read, or None if you cannot express one."""
```

Either can add `async` versions (`asearch`, `acheck`, `afilter_for`); otherwise the sync methods
run in a thread under `aretrieve`. `FunctionAuthorizer` wraps a plain function.

## Testing approach

The guarantees are tested as properties with [Hypothesis](https://hypothesis.readthedocs.io):
for randomly generated corpora, permissions, users and queries, every strategy must

1. never return a chunk the user may not read,
2. return exactly the top-k of what the user may read (checked against an independent oracle),
3. stay safe when the backend ignores the filter, and
4. give identical results through `retrieve` and `aretrieve`.

## Roadmap to 1.0

`needtoknow` is in active development towards a 1.0 release with a stable API.

- [x] Core: filters, strategies, failing closed, re-checks, async
- [x] Backends: pgvector, Chroma
- [x] Authorizers: OPA, OpenFGA
- [x] Frameworks: LangChain and LlamaIndex retrievers
- [x] Permission caching with a documented revocation bound, OpenTelemetry hooks, audit events
- [ ] Benchmarks with real embeddings; documentation site

After 1.0: permission sync from Google Drive, SharePoint and Confluence; Qdrant, Elasticsearch,
SpiceDB and Cerbos adapters. Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Example deployment

[`examples/gateway`](examples/gateway) is a complete, self-hosted, OpenAI-compatible gateway built
on the same ideas: OPA policies, pgvector hybrid search with the ACL inside the query, PII
pseudonymisation, prompt-injection quarantine and a hash-chained audit log. It is being ported onto
this library.

## License

[MIT](LICENSE)
