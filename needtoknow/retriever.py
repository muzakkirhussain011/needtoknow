"""PermissionedRetriever: search, but only return what the subject may read.

Strategies
----------
prefilter
    Ask the authorizer for a filter and let the backend apply it inside the search. Results are
    complete (the top k of what the subject may read) and cost one search. Needs an authorizer that
    can express its rules as a filter and a backend that supports filters.
postfilter
    Search without a filter, then check each candidate. Fetches `k * overfetch` candidates and
    widens the search until k permitted hits are found, the backend runs out, or `max_candidates`
    is reached. Works with any authorizer; for subjects who may read little, results can be
    incomplete, which `RetrievalResult.complete` reports.
auto (default)
    prefilter when both sides support it, postfilter otherwise.

Guarantees
----------
* Fail closed: if the authorizer errors, `AuthorizationError` is raised and nothing is returned.
* Defence in depth: pre-filtered results are checked again (`recheck=True`). A backend that
  ignored the filter cannot leak; the rejected hits are counted in `violations`.

The algorithm is written once, as a generator of operations; `retrieve` runs it synchronously and
`aretrieve` runs it with `await`, so both paths share the same security logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from needtoknow.authz import AuthorizationError, Authorizer
from needtoknow.filters import Filter
from needtoknow.types import Hit, Subject

__all__ = ["PermissionedRetriever", "RetrievalResult", "SearchBackend", "Strategy"]

Strategy = Literal["auto", "prefilter", "postfilter"]


@runtime_checkable
class SearchBackend(Protocol):
    """A vector store or search index. Return hits ordered best first.

    `supports_filters` says whether `search` honours the `filter` argument. Backends may also
    provide `async def asearch(...)` with the same signature; otherwise `search` runs in a thread.
    """

    supports_filters: bool

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]: ...


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[Hit]
    """Permitted hits, best first, at most k."""
    strategy: Literal["prefilter", "postfilter"]
    """The strategy that was actually used."""
    examined: int
    """Candidates the authorizer checked."""
    denied: int
    """Post-filtered candidates the subject may not read."""
    violations: int
    """Pre-filtered hits that failed the re-check. Should always be 0; anything else means the
    backend did not apply the filter and deserves an alert."""
    complete: bool
    """True when `hits` is the full top-k of what the subject may read."""


# Operations the algorithm asks its driver to perform.
@dataclass(frozen=True)
class _FilterFor:
    pass


@dataclass(frozen=True)
class _Search:
    query: str
    k: int
    filter: Filter | None


@dataclass(frozen=True)
class _Check:
    hits: list[Hit]


_Op = _FilterFor | _Search | _Check
_Plan = Generator[_Op, Any, RetrievalResult]


class PermissionedRetriever:
    def __init__(
        self,
        backend: SearchBackend,
        authorizer: Authorizer,
        *,
        strategy: Strategy = "auto",
        action: str = "read",
        overfetch: int = 4,
        max_candidates: int = 1000,
        recheck: bool = True,
    ) -> None:
        if strategy not in ("auto", "prefilter", "postfilter"):
            raise ValueError(f"unknown strategy {strategy!r}")
        if strategy == "prefilter" and not backend.supports_filters:
            raise ValueError("strategy='prefilter' needs a backend that supports filters")
        if overfetch < 2:
            raise ValueError("overfetch must be at least 2")
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        self.backend = backend
        self.authorizer = authorizer
        self.strategy: Strategy = strategy
        self.action = action
        self.overfetch = overfetch
        self.max_candidates = max_candidates
        self.recheck = recheck

    # ---- public API ------------------------------------------------------------------------

    def retrieve(self, query: str, subject: Subject, k: int = 5) -> RetrievalResult:
        plan = self._plan(query, subject, k)
        try:
            op = next(plan)
            while True:
                op = plan.send(self._run(op, subject))
        except StopIteration as done:
            return done.value

    async def aretrieve(self, query: str, subject: Subject, k: int = 5) -> RetrievalResult:
        plan = self._plan(query, subject, k)
        try:
            op = next(plan)
            while True:
                op = plan.send(await self._arun(op, subject))
        except StopIteration as done:
            return done.value

    # ---- the algorithm (shared by both drivers) --------------------------------------------

    def _plan(self, query: str, subject: Subject, k: int) -> _Plan:
        if k < 1:
            raise ValueError("k must be at least 1")
        f: Filter | None = None
        if self.strategy != "postfilter" and self.backend.supports_filters:
            f = yield _FilterFor()
            if f is None and self.strategy == "prefilter":
                raise ValueError("strategy='prefilter' needs an authorizer that returns a filter")
        if f is not None:
            return (yield from self._prefilter(query, k, f))
        return (yield from self._postfilter(query, k))

    def _prefilter(self, query: str, k: int, f: Filter) -> _Plan:
        hits: list[Hit] = yield _Search(query, k, f)
        hits = list(hits)[:k]
        if not self.recheck or not hits:
            return RetrievalResult(hits, "prefilter", examined=0, denied=0, violations=0, complete=True)
        decisions: list[bool] = yield _Check(hits)
        kept = [h for h, ok in zip(hits, decisions, strict=True) if ok]
        violations = len(hits) - len(kept)
        return RetrievalResult(
            kept, "prefilter", examined=len(hits), denied=0, violations=violations, complete=violations == 0
        )

    def _postfilter(self, query: str, k: int) -> _Plan:
        fetch = min(k * self.overfetch, self.max_candidates)
        seen: set[str] = set()
        kept: list[Hit] = []
        examined = denied = 0
        while True:
            candidates: list[Hit] = yield _Search(query, fetch, None)
            fresh = [h for h in candidates if h.id not in seen]
            seen.update(h.id for h in fresh)
            if fresh:
                decisions: list[bool] = yield _Check(fresh)
                for hit, ok in zip(fresh, decisions, strict=True):
                    if ok:
                        kept.append(hit)
                    else:
                        denied += 1
                examined += len(fresh)
            exhausted = len(candidates) < fetch
            if len(kept) >= k or exhausted or fetch >= self.max_candidates:
                break
            fetch = min(fetch * self.overfetch, self.max_candidates)
        kept.sort(key=lambda h: h.score, reverse=True)  # stable: equal scores keep backend order
        return RetrievalResult(
            kept[:k],
            "postfilter",
            examined=examined,
            denied=denied,
            violations=0,
            complete=len(kept) >= k or exhausted,
        )

    # ---- drivers ---------------------------------------------------------------------------

    def _run(self, op: _Op, subject: Subject) -> Any:
        if isinstance(op, _Search):
            return self.backend.search(op.query, op.k, op.filter)
        try:
            if isinstance(op, _FilterFor):
                return self.authorizer.filter_for(subject, self.action)
            return self._validated(op, self.authorizer.check(subject, op.hits, self.action))
        except AuthorizationError:
            raise
        except Exception as exc:  # fail closed: any authorizer failure means no results
            raise AuthorizationError(f"authorizer failed: {exc}") from exc

    async def _arun(self, op: _Op, subject: Subject) -> Any:
        if isinstance(op, _Search):
            asearch = getattr(self.backend, "asearch", None)
            if asearch is not None:
                return await asearch(op.query, op.k, op.filter)
            return await asyncio.to_thread(self.backend.search, op.query, op.k, op.filter)
        try:
            if isinstance(op, _FilterFor):
                afilter_for = getattr(self.authorizer, "afilter_for", None)
                if afilter_for is not None:
                    return await afilter_for(subject, self.action)
                return await asyncio.to_thread(self.authorizer.filter_for, subject, self.action)
            acheck = getattr(self.authorizer, "acheck", None)
            if acheck is not None:
                decisions = await acheck(subject, op.hits, self.action)
            else:
                decisions = await asyncio.to_thread(self.authorizer.check, subject, op.hits, self.action)
            return self._validated(op, decisions)
        except AuthorizationError:
            raise
        except Exception as exc:
            raise AuthorizationError(f"authorizer failed: {exc}") from exc

    @staticmethod
    def _validated(op: _Check, decisions: Sequence[bool]) -> list[bool]:
        decisions = list(decisions)
        if len(decisions) != len(op.hits):
            raise AuthorizationError(f"authorizer returned {len(decisions)} decisions for {len(op.hits)} hits")
        if not all(isinstance(d, bool) for d in decisions):
            raise AuthorizationError("authorizer decisions must be booleans")
        return decisions
