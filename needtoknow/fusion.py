"""Hybrid search: the results of several backends, fused by reciprocal rank fusion.

    vector = ElasticsearchBackend(client, "docs", embed)
    keyword = ElasticsearchBackend(client, "docs", None, mode="keyword")
    retriever = PermissionedRetriever(RankFusion(vector, keyword), authorizer)
    retriever.retrieve("travel policy", subject)  # or Query(text=..., vector=...) to embed it yourself

Every backend is searched with the filter the retriever passes, so each fused chunk came from a
backend that applied the permission filter, and the retriever still checks the results as it does
for any backend. A chunk's score is the sum, over the backends that returned it, of
`weight / (rank_constant + rank)`; scores from different backends never need to be comparable.

A chunk that two backends return with different metadata (for example a keyword index not yet
updated after a permission change) is left out and logged: its permissions are uncertain, so it is
never returned with whichever version came first.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from typing import Any

from needtoknow.filters import Filter
from needtoknow.retriever import SearchBackend
from needtoknow.types import Hit, Query

__all__ = ["RankFusion"]

_log = logging.getLogger("needtoknow")


async def _asearch(backend: SearchBackend, query: str | Query, k: int, filter: Filter | None) -> list[Hit]:
    native = getattr(backend, "asearch", None)
    if native is not None:
        return list(await native(query, k, filter))
    return list(await asyncio.to_thread(backend.search, query, k, filter))


class RankFusion:
    """A backend that searches several backends and fuses their rankings (reciprocal rank fusion).

    `weights` (one per backend, default 1 each) scale each backend's contribution.
    `rank_constant` (default 60, as in the original RRF paper) damps the advantage of the very top
    ranks. `depth` is how many results each backend is asked for (default: the `k` requested).
    Filters are supported when every backend supports them. Every backend gets the same query: a
    `Query` with both `text` and `vector` gives keyword backends the text and vector backends the
    vector, so the query is embedded once, by you, however many vector backends there are.
    """

    def __init__(
        self,
        *backends: SearchBackend,
        weights: Sequence[float] | None = None,
        rank_constant: float = 60.0,
        depth: int | None = None,
    ) -> None:
        if not backends:
            raise ValueError("RankFusion needs at least one backend")
        chosen = [1.0] * len(backends) if weights is None else [float(w) for w in weights]
        if len(chosen) != len(backends) or not all(math.isfinite(w) and w > 0 for w in chosen):
            raise ValueError("weights must be one positive number per backend")
        if not (math.isfinite(rank_constant) and rank_constant >= 0):
            raise ValueError("rank_constant must be a non-negative number")
        if depth is not None and depth < 1:
            raise ValueError("depth must be positive")
        self.backends = backends
        self.weights = chosen
        self.rank_constant = rank_constant
        self.depth = depth
        self.supports_filters = all(getattr(b, "supports_filters", False) for b in backends)

    def _fuse(self, rankings: Sequence[Sequence[Hit]], k: int) -> list[Hit]:
        scores: dict[str, float] = {}
        first: dict[str, Hit] = {}
        conflicting: set[str] = set()
        for weight, hits in zip(self.weights, rankings, strict=True):
            rank = 0
            seen: set[str] = set()
            for hit in hits:
                earlier = first.setdefault(hit.id, hit)
                if earlier.metadata != hit.metadata:
                    conflicting.add(hit.id)
                if hit.id in seen:  # a backend that repeats a chunk counts it once, at its best rank
                    continue
                seen.add(hit.id)
                rank += 1
                scores[hit.id] = scores.get(hit.id, 0.0) + weight / (self.rank_constant + rank)
        if conflicting:
            _log.warning(
                "needtoknow RankFusion left out %d chunks whose metadata differs between backends", len(conflicting)
            )
        fused = [
            Hit(doc_id, first[doc_id].text, score, first[doc_id].metadata)
            for doc_id, score in scores.items()
            if doc_id not in conflicting
        ]
        fused.sort(key=lambda h: (-h.score, h.id))
        return fused[:k]

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        depth = self.depth or k
        return self._fuse([list(b.search(query, depth, filter)) for b in self.backends], k)

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        depth = self.depth or k
        rankings: list[Any] = await asyncio.gather(*(_asearch(b, query, depth, filter) for b in self.backends))
        return self._fuse(rankings, k)
