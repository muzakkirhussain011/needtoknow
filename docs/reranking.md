# Reranking

A reranker, such as a cross-encoder or a hosted rerank API, reads the query and each candidate
chunk together and orders them better than vector similarity can. Give it to the retriever, and it
runs after the permission check:

```python
from sentence_transformers import CrossEncoder

from wardowl import Hit, PermissionedRetriever, Query

model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L6-v2")


def rerank(query, hits):
    text = Query.of(query).keywords()  # the query's text, whether a string or a Query was passed
    scores = model.predict([(text, h.text) for h in hits])
    ranked = sorted(zip(hits, scores), key=lambda pair: -pair[1])
    return [Hit(h.id, h.text, float(score), h.metadata) for h, score in ranked]


retriever = PermissionedRetriever(backend, authorizer, reranker=rerank, rerank_depth=50)
result = retriever.retrieve("travel policy for contractors", subject, k=5)
```

The retriever finds the top `rerank_depth` chunks the subject may read (default 50, or k if that is
larger), the reranker orders them, and the first k are returned. Any model works: the retriever
only needs a function, or an object with a `rerank` method, that takes the query and a list of
`Hit`s and returns some or all of them in its order.

## Why the order matters

Reranking before the permission check would send every candidate, including chunks the user may not
read, to the reranker. With a hosted rerank API that means sending them to a third party; with any
reranker it means a model saw text it should not have, and could surface it in its scores or logs.
wardowl reranks only what passed the check:

- The reranker receives only hits the authorizer allowed (and, when pre-filtering, the re-check
  confirmed).
- It can reorder, rescore and drop hits, but not add any. A hit it was not given, a hit returned
  twice, or a score that is not a finite number raises `ValueError`, and nothing is returned.
- Returned hits keep the text and metadata they were stored with; only the reranker's score is
  taken from its answer.
- If the authorizer fails, retrieval stops with `AuthorizationError` before the reranker is called.

## Async and failures

For `aretrieve`, an object with `async def arerank(query, hits)` is awaited; otherwise `rerank` runs
in a thread. An exception from the reranker, such as a timeout from a rerank service, reaches the
caller unchanged. To fall back to the retrieval order instead, catch it in your function and return
the hits as they came:

```python
def rerank_or_keep(query, hits):
    try:
        return rerank(query, hits)
    except TimeoutError:
        return hits
```

The hits it falls back to are the permitted ones, so a fallback never weakens permissions.

## Text and vector queries

The reranker gets the query exactly as it was given to `retrieve`: a string, or a
[`Query`](vectors.md). Cross-encoders need text; `Query.of(query).keywords()` gives it, as in the
example above, and raises `ValueError` for a query that has only a vector.

## Monitoring

`RetrievalEvent.reranked` is the number of chunks given to the reranker (0 without one) and
`rerank_ms` the time it took. `OpenTelemetryObserver` adds them to the span, and
`OpenTelemetryMetrics` records them in the `wardowl.rerank.duration` histogram. See
[Operations](operations.md).

`RetrievalResult.complete` describes the pool the reranker chose from: whether those were the top
`rerank_depth` chunks the subject may read.
