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

backend = InMemoryBackend([
    Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
    Document("salaries", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]}),
    Document("runbook", "Roll back a failed deploy ...", {"allowed_groups": ["engineering"]}),
])
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

## Bring your own backend or permission system

A backend is any object with a `search` method:

```python
class MyBackend:
    supports_filters = True  # set False if search() ignores `filter`

    def search(self, query: str, k: int, filter=None) -> list[Hit]:
        ...  # translate `filter` (In / And / Or) into your store's native filter
```

An authorizer answers two questions:

```python
class MyAuthorizer:
    def check(self, subject, hits, action="read") -> list[bool]:
        ...  # one decision per hit

    def filter_for(self, subject, action="read"):
        ...  # a Filter selecting everything the subject may read, or None
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
- [ ] Backends: pgvector, Chroma
- [ ] Authorizers: OpenFGA, OPA
- [ ] Frameworks: LangChain and LlamaIndex retrievers
- [ ] Permission caching with a documented revocation bound, OpenTelemetry hooks, audit events
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
