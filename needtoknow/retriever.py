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
* Defence in depth: pre-filtered results are checked again with the authorizer (`recheck=True`).
  A backend that ignored the filter cannot leak; the rejected hits are counted in `violations`.
  With `recheck=False` there is no second call to the authorizer, but each pre-filtered hit is
  still matched against the filter locally, so a backend that ignores the filter cannot leak
  either; only a filter that is itself broader than the authorizer's check goes unnoticed.
* A `reranker` sees only hits that passed the check, and can only reorder, score or drop them.

The algorithm is written once, as a generator of operations; `retrieve` runs it synchronously and
`aretrieve` runs it with `await`, so both paths share the same security logic.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from needtoknow.authz import AuthorizationError, Authorizer
from needtoknow.events import Observer, RetrievalEvent, query_fingerprint
from needtoknow.filters import And, Filter, In, Or, matches
from needtoknow.types import Hit, Query, Subject

__all__ = ["PermissionedRetriever", "Reranker", "RetrievalResult", "SearchBackend", "Strategy"]

Strategy = Literal["auto", "prefilter", "postfilter"]


@runtime_checkable
class SearchBackend(Protocol):
    """A vector store or search index. Return hits ordered best first.

    `supports_filters` says whether `search` honours the `filter` argument. Backends may also
    provide `async def asearch(...)` with the same signature; otherwise `search` runs in a thread.

    `query` is what the caller passed to `retrieve`, unchanged: a string, or a `Query` when the
    caller supplies a vector. `Query.of(query)` gives a `Query` either way. A backend that takes
    only strings still works with every string query.
    """

    supports_filters: bool

    def search(self, query: Any, k: int, filter: Filter | None = None) -> list[Hit]: ...


@runtime_checkable
class Reranker(Protocol):
    """Reorders the hits a subject may read: a cross-encoder, a hosted rerank API, any model.

    `rerank` receives the query as it was given to `retrieve` and the permitted hits, best first,
    and returns some or all of those hits in its order, with its scores. It may also provide
    `async def arerank(...)` with the same signature. A plain function with the same signature
    can be passed instead.
    """

    def rerank(self, query: Any, hits: list[Hit]) -> Sequence[Hit]: ...


@dataclass(frozen=True)
class RetrievalResult:
    """What one retrieval returned, with the counts needed to monitor it."""

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
    """False when needtoknow knows results may be missing: post-filtering stopped at
    `max_candidates`, or the re-check removed pre-filtered hits. It trusts the backend's ranking,
    so it cannot see results an approximate index failed to return."""


# Operations the algorithm asks its driver to perform.
@dataclass(frozen=True)
class _FilterFor:
    pass


@dataclass(frozen=True)
class _ScopeFor:
    pass


@dataclass(frozen=True)
class _Search:
    query: str | Query
    k: int
    filter: Filter | None


@dataclass(frozen=True)
class _Check:
    hits: list[Hit]


@dataclass(frozen=True)
class _Rerank:
    query: str | Query
    hits: list[Hit]


_Op = _FilterFor | _ScopeFor | _Search | _Check | _Rerank
_log = logging.getLogger("needtoknow")
_Plan = Generator[_Op, Any, RetrievalResult]
RerankFunction = Callable[[Any, list[Hit]], Sequence[Hit]]


class _Timings:
    """Seconds spent in the backend, the authorizer and the reranker during one retrieval."""

    def __init__(self) -> None:
        self.search = self.authorize = self.rerank = 0.0
        self.searches = self.reranked = 0

    @contextmanager
    def measure(self, op: _Op) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:  # a failed operation still took its time
            elapsed = time.perf_counter() - start
            if isinstance(op, _Search):
                self.search += elapsed
                self.searches += 1
            elif isinstance(op, _Rerank):
                self.rerank += elapsed
                self.reranked += len(op.hits)
            else:
                self.authorize += elapsed


class PermissionedRetriever:
    """Searches a backend and returns only the hits the subject may read.

    `strategy` chooses where permissions are applied: `"prefilter"` inside the search, with a
    filter from the authorizer; `"postfilter"` after it, checking each candidate; `"auto"` (the
    default) pre-filters when the backend supports filters and the authorizer returns one.
    `action` is passed to the authorizer on every call.

    Post-filtering fetches `k * overfetch` candidates and multiplies the fetch by `overfetch`
    until k permitted hits are found, the backend has no more, or `max_candidates` is reached.
    `recheck` checks pre-filtered hits again with the authorizer, so a backend that ignores the
    filter cannot leak; with `recheck=False` they are matched against the filter locally instead,
    which needs the backend to return each chunk's metadata as stored (the conformance kit checks
    that). `observers` receive a `RetrievalEvent` for every call, and `audit_key` adds an HMAC of
    the query to those events.

    `reranker` reorders the result after the permission check: the retriever finds the top
    `rerank_depth` hits the subject may read (or k, if larger), the reranker orders them, and the
    first k are returned. The reranker never sees a chunk the subject may not read.
    """

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
        observers: Sequence[Observer] = (),
        audit_key: bytes | None = None,
        reranker: Reranker | RerankFunction | None = None,
        rerank_depth: int = 50,
    ) -> None:
        if strategy not in ("auto", "prefilter", "postfilter"):
            raise ValueError(f"unknown strategy {strategy!r}")
        if strategy == "prefilter" and not backend.supports_filters:
            raise ValueError("strategy='prefilter' needs a backend that supports filters")
        if overfetch < 2:
            raise ValueError("overfetch must be at least 2")
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        if reranker is not None and not (hasattr(reranker, "rerank") or callable(reranker)):
            raise TypeError("reranker must have a rerank method or be a function")
        if rerank_depth < 1:
            raise ValueError("rerank_depth must be positive")
        self.reranker = reranker
        self.rerank_depth = rerank_depth
        self.backend = backend
        self.authorizer = authorizer
        self.strategy: Strategy = strategy
        self.action = action
        self.overfetch = overfetch
        self.max_candidates = max_candidates
        self.recheck = recheck
        self.observers = tuple(observers)
        self.audit_key = audit_key

    # ---- public API ------------------------------------------------------------------------

    def retrieve(self, query: str | Query, subject: Subject, k: int = 5) -> RetrievalResult:
        """Return at most `k` hits the subject may read, best first.

        `query` is text, or a `Query` carrying a vector you computed. Raises `AuthorizationError`
        when the authorizer fails or gives a malformed answer, and `ValueError` or `TypeError`
        for invalid arguments. Nothing is returned in any of these cases.
        """
        started, clock, timings = time.time(), time.perf_counter(), _Timings()
        try:
            plan = self._plan(query, subject, k)
            try:
                op = next(plan)
                while True:
                    with timings.measure(op):
                        value = self._run(op, subject)
                    op = plan.send(value)
            except StopIteration as done:
                result: RetrievalResult = done.value
        except BaseException as exc:  # a cancelled or interrupted call is reported too, then re-raised
            self._emit(query, subject, k, started, clock, timings, None, exc)
            raise
        self._emit(query, subject, k, started, clock, timings, result, None)
        return result

    async def aretrieve(self, query: str | Query, subject: Subject, k: int = 5) -> RetrievalResult:
        """The async form of `retrieve`, with the same arguments and guarantees.

        It uses the backend's `asearch` and the authorizer's `afilter_for` and `acheck` when they
        exist, and runs the sync methods in a thread otherwise.
        """
        started, clock, timings = time.time(), time.perf_counter(), _Timings()
        try:
            plan = self._plan(query, subject, k)
            try:
                op = next(plan)
                while True:
                    with timings.measure(op):
                        value = await self._arun(op, subject)
                    op = plan.send(value)
            except StopIteration as done:
                result: RetrievalResult = done.value
        except BaseException as exc:  # a cancelled or interrupted call is reported too, then re-raised
            self._emit(query, subject, k, started, clock, timings, None, exc)
            raise
        self._emit(query, subject, k, started, clock, timings, result, None)
        return result

    def _emit(
        self,
        query: str | Query,
        subject: Subject,
        k: int,
        started: float,
        clock: float,
        timings: _Timings,
        result: RetrievalResult | None,
        error: BaseException | None,
    ) -> None:
        if not self.observers:
            return
        try:
            event = self._event(query, subject, k, started, clock, timings, result, error)
        except Exception:  # reporting must never replace what retrieval returns or raises
            _log.exception("needtoknow could not build the retrieval event")
            return
        for observer in self.observers:
            try:
                observer(event)
            except Exception:  # an observer must never change what retrieval returns or raises
                _log.exception("needtoknow observer %r failed", observer)

    def _event(
        self,
        query: str | Query,
        subject: Subject,
        k: int,
        started: float,
        clock: float,
        timings: _Timings,
        result: RetrievalResult | None,
        error: BaseException | None,
    ) -> RetrievalEvent:
        fingerprint = None
        if self.audit_key and isinstance(query, (str, Query)):  # a query of the wrong type is reported, not hashed
            fingerprint = query_fingerprint(query, self.audit_key)
        return RetrievalEvent(
            subject_id=subject.id,
            action=self.action,
            k=k,
            strategy=result.strategy if result else None,
            returned_ids=tuple(h.id for h in result.hits) if result else (),
            examined=result.examined if result else 0,
            denied=result.denied if result else 0,
            violations=result.violations if result else 0,
            complete=result.complete if result else False,
            duration_ms=round((time.perf_counter() - clock) * 1000, 3),
            error=type(error).__name__ if error else None,
            cause=type(error.__cause__).__name__ if error is not None and error.__cause__ is not None else None,
            query_hmac=fingerprint,
            started_at=started,
            search_ms=round(timings.search * 1000, 3),
            authorize_ms=round(timings.authorize * 1000, 3),
            searches=timings.searches,
            rerank_ms=round(timings.rerank * 1000, 3),
            reranked=timings.reranked,
        )

    # ---- the algorithm (shared by both drivers) --------------------------------------------

    def _plan(self, query: str | Query, subject: Subject, k: int) -> _Plan:
        if k < 1:
            raise ValueError("k must be at least 1")
        if not isinstance(query, (str, Query)):
            raise TypeError(f"a query is a string or a Query, not {type(query).__name__}")
        if self.reranker is None:
            return (yield from self._permitted(query, k))
        result = yield from self._permitted(query, max(k, self.rerank_depth))
        if not result.hits:
            return result
        # Only hits that passed the check reach the reranker, which can only reorder, score or drop them.
        ranked: list[Hit] = yield _Rerank(query, result.hits)
        return dataclasses.replace(result, hits=ranked[:k])

    def _permitted(self, query: str | Query, k: int) -> _Plan:
        """The top k hits the subject may read."""
        f: Filter | None = None
        if self.strategy != "postfilter" and self.backend.supports_filters:
            f = yield _FilterFor()
            if f is None and self.strategy == "prefilter":
                raise ValueError("strategy='prefilter' needs an authorizer that returns a filter")
        if f is not None:
            return (yield from self._prefilter(query, k, f))
        scope: Filter | None = None
        if self.backend.supports_filters and hasattr(self.authorizer, "scope_for"):
            # A necessary condition without the full answer (for example the subject's tenant):
            # post-filtering then searches only inside it, and still checks every candidate.
            scope = yield _ScopeFor()
        return (yield from self._postfilter(query, k, scope))

    def _prefilter(self, query: str | Query, k: int, f: Filter) -> _Plan:
        hits: list[Hit] = yield _Search(query, k, f)
        hits = list(hits)[:k]
        if not hits:
            return RetrievalResult(hits, "prefilter", examined=0, denied=0, violations=0, complete=True)
        if self.recheck:
            decisions: list[bool] = yield _Check(hits)
            kept = [h for h, ok in zip(hits, decisions, strict=True) if ok]
        else:
            # No call to the authorizer, but each hit must still match the filter the backend was
            # given: one that does not means the backend ignored it, and is dropped and counted.
            kept = [h for h in hits if matches(f, h.metadata)]
        violations = len(hits) - len(kept)
        examined = len(hits) if self.recheck else 0
        return RetrievalResult(
            kept, "prefilter", examined=examined, denied=0, violations=violations, complete=violations == 0
        )

    def _postfilter(self, query: str | Query, k: int, scope: Filter | None = None) -> _Plan:
        fetch = min(k * self.overfetch, self.max_candidates)
        seen: set[str] = set()
        kept: list[Hit] = []
        examined = denied = 0
        while True:
            candidates: list[Hit] = yield _Search(query, fetch, scope)
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
        if isinstance(op, _Rerank):
            rerank: Any = getattr(self.reranker, "rerank", self.reranker)
            return self._reranked(op.hits, rerank(op.query, list(op.hits)))
        try:
            if isinstance(op, _FilterFor):
                return self._filter(self.authorizer.filter_for(subject, self.action))
            if isinstance(op, _ScopeFor):
                return self._filter(self.authorizer.scope_for(subject, self.action))  # type: ignore[attr-defined]
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
        if isinstance(op, _Rerank):
            arerank = getattr(self.reranker, "arerank", None)
            if arerank is not None:
                return self._reranked(op.hits, await arerank(op.query, list(op.hits)))
            rerank: Any = getattr(self.reranker, "rerank", self.reranker)
            return self._reranked(op.hits, await asyncio.to_thread(rerank, op.query, list(op.hits)))
        try:
            if isinstance(op, (_FilterFor, _ScopeFor)):
                name = "filter_for" if isinstance(op, _FilterFor) else "scope_for"
                native = getattr(self.authorizer, f"a{name}", None)
                if native is not None:
                    return self._filter(await native(subject, self.action))
                return self._filter(await asyncio.to_thread(getattr(self.authorizer, name), subject, self.action))
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
    def _filter(value: Any) -> Filter | None:
        if value is None or isinstance(value, (In, And, Or)):
            return value
        raise AuthorizationError(f"authorizer returned {type(value).__name__}, not a filter")

    @staticmethod
    def _reranked(hits: list[Hit], ranked: Sequence[Hit]) -> list[Hit]:
        """The reranker's order and scores, applied to the chunks it was given, exactly as they were."""
        given = {h.id: h for h in hits}
        reordered: list[Hit] = []
        for hit in ranked:
            doc_id = getattr(hit, "id", None)
            original = given.pop(doc_id, None) if isinstance(doc_id, str) else None
            if original is None:
                raise ValueError("the reranker returned a chunk it was not given, or one chunk twice")
            score = float(hit.score)
            if not math.isfinite(score):
                raise ValueError("the reranker returned a score that is not a finite number")
            reordered.append(Hit(original.id, original.text, score, original.metadata))
        return reordered

    @staticmethod
    def _validated(op: _Check, decisions: Sequence[bool]) -> list[bool]:
        decisions = list(decisions)
        if len(decisions) != len(op.hits):
            raise AuthorizationError(f"authorizer returned {len(decisions)} decisions for {len(op.hits)} hits")
        if not all(isinstance(d, bool) for d in decisions):
            raise AuthorizationError("authorizer decisions must be booleans")
        return decisions
