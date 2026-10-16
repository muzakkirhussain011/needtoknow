"""CachingAuthorizer: fewer calls to the permission service, with a stated revocation bound.

Wrap any authorizer:

    authorizer = CachingAuthorizer(OpenFGAAuthorizer(...), ttl=30)

Revocation bound: a permission revoked in the source of truth stops granting access at most `ttl`
seconds later (plus whatever delay the permission service itself has). Call `invalidate()` after
a change you know about to drop cached decisions at once.

What is cached, per (subject, action): the pre-filter from `filter_for`, and each `check` decision,
keyed by the chunk id and its metadata, so a chunk whose metadata changes is decided again.
Errors are never cached: every failure reaches the caller and retrieval fails closed.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

from needtoknow.authz import Authorizer
from needtoknow.filters import Filter
from needtoknow.types import Hit, Subject

__all__ = ["CachingAuthorizer"]

_NONE = object()  # cached "this authorizer returns no filter"


def _hit_key(hit: Hit) -> tuple[str, str]:
    try:
        metadata = json.dumps(dict(hit.metadata), sort_keys=True, default=repr)
    except (TypeError, ValueError):
        metadata = repr(sorted(hit.metadata.items(), key=repr))
    return hit.id, metadata


class CachingAuthorizer:
    def __init__(
        self,
        authorizer: Authorizer,
        *,
        ttl: float = 30.0,
        maxsize: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0 or maxsize < 1:
            raise ValueError("ttl and maxsize must be positive")
        self.authorizer = authorizer
        self.ttl = ttl
        self.maxsize = maxsize
        self._clock = clock
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    # ---- cache primitives ------------------------------------------------------------------

    def _get(self, key: Any) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= self._clock():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def _put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self.ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def invalidate(self, subject: Subject | None = None) -> None:
        """Drop every cached decision, or only those for `subject`."""
        with self._lock:
            if subject is None:
                self._entries.clear()
            else:
                for key in [k for k in self._entries if k[1] == subject]:
                    del self._entries[key]

    # ---- Authorizer ------------------------------------------------------------------------

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        key = ("filter", subject, action)
        cached = self._get(key)
        if cached is not None:
            return None if cached is _NONE else cached
        result = self.authorizer.filter_for(subject, action)
        self._put(key, _NONE if result is None else result)
        return result

    def _split(self, subject: Subject, hits: Sequence[Hit], action: str) -> tuple[list[Any], list[int]]:
        keys = [("check", subject, action, _hit_key(h)) for h in hits]
        decisions = [self._get(k) for k in keys]
        return [keys, decisions], [i for i, d in enumerate(decisions) if d is None]

    def _merge(self, parts: list[Any], missing: list[int], fresh: Sequence[bool]) -> list[bool]:
        keys, decisions = parts
        fresh = list(fresh)
        if len(fresh) != len(missing):
            # Leave it to the retriever's validation to reject; never cache a malformed answer.
            return fresh
        for i, decision in zip(missing, fresh, strict=True):
            if isinstance(decision, bool):
                self._put(keys[i], decision)
            decisions[i] = decision
        return decisions

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        parts, missing = self._split(subject, hits, action)
        if not missing:
            return parts[1]
        fresh = self.authorizer.check(subject, [hits[i] for i in missing], action)
        return self._merge(parts, missing, fresh)

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        key = ("filter", subject, action)
        cached = self._get(key)
        if cached is not None:
            return None if cached is _NONE else cached
        inner = getattr(self.authorizer, "afilter_for", None)
        if inner is not None:
            result = await inner(subject, action)
        else:
            result = await asyncio.to_thread(self.authorizer.filter_for, subject, action)
        self._put(key, _NONE if result is None else result)
        return result

    async def acheck(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        parts, missing = self._split(subject, hits, action)
        if not missing:
            return parts[1]
        todo = [hits[i] for i in missing]
        inner = getattr(self.authorizer, "acheck", None)
        if inner is not None:
            fresh = await inner(subject, todo, action)
        else:
            fresh = await asyncio.to_thread(self.authorizer.check, subject, todo, action)
        return self._merge(parts, missing, fresh)
