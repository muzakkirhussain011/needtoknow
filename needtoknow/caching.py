"""CachingAuthorizer: fewer calls to the permission service, with a stated revocation bound.

Wrap any authorizer:

    authorizer = CachingAuthorizer(OpenFGAAuthorizer(...), ttl=30)

Revocation bound: a permission revoked in the source of truth stops granting access at most `ttl`
seconds later (plus whatever delay the permission service itself has). Call `invalidate()` after
a change you know about to drop cached decisions at once. An answer the authorizer gave before that
call, to a retrieval still in progress, is not cached after it either.

What is cached, per (subject, action): the pre-filter from `filter_for`, and each `check` decision,
keyed by the chunk id and its metadata, so a chunk whose metadata changes is decided again.
Errors are never cached: every failure reaches the caller and retrieval fails closed.

Across processes: pass `shared=RedisCache(redis_client)`, or any `SharedCache`, and every replica
uses the answers any of them obtained. Each process then keeps its own copy of an answer for at
most `local_ttl` seconds (1 by default) before reading the shared cache again, so `invalidate()` in
one replica reaches every other one within `local_ttl`. Each shared entry carries its expiry time,
so the revocation bound holds however many replicas copy it (with clocks kept in sync, as NTP
does). `invalidate()` also replaces an epoch stored in the shared cache, and an entry is used only
under the epoch it was written in, so an answer another replica obtained before the invalidation
and writes after it is never read. A shared cache that fails is treated as a miss: the authorizer
is asked, and the cache is never the reason access is granted.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from needtoknow.authz import AuthorizationError, Authorizer
from needtoknow.filters import Filter, filter_from_dict, filter_to_dict
from needtoknow.types import Hit, Subject

__all__ = ["CachingAuthorizer", "RedisCache", "SharedCache"]

_NONE = object()  # cached "this authorizer returns no filter"
_MISS = object()  # a shared entry that is absent, expired, unreadable, or from an earlier epoch
_UNKNOWN = object()  # the shared epochs could not be read, so nothing learned is written there
# How much longer than `ttl` an epoch is kept. Until it expires, entries written under the epoch it
# replaced are refused; by then they have long expired, so "no epoch yet" cannot bring one back.
_EPOCH_MARGIN = 3600.0
_log = logging.getLogger("needtoknow")


def _json_or_repr(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(sorted(value.items(), key=repr)) if isinstance(value, Mapping) else repr(value)


def _hit_key(hit: Hit) -> tuple[str, str]:
    return hit.id, _json_or_repr(dict(hit.metadata))


def _digest(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    for part in parts:
        h.update(part.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()


def _subject_digest(subject: Subject) -> str:
    groups = sorted(subject.groups)
    return _digest(subject.id, str(len(groups)), *groups, _json_or_repr(dict(subject.attributes)))


def _epoch_id(blob: Any) -> str | None:
    """The epoch a shared cache holds, as text to compare; None when it holds none yet."""
    if blob is None:
        return None
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"a shared cache returned {type(blob).__name__}, not bytes")
    return bytes(blob).hex()


@runtime_checkable
class SharedCache(Protocol):
    """A cache every process shares, such as Redis. Keys are strings and values opaque bytes."""

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        """The value of each key, or None when it is absent."""
        ...

    def set_many(self, entries: Mapping[str, bytes], ttl: float) -> None:
        """Store each entry for `ttl` seconds."""
        ...

    def delete_prefix(self, prefix: str) -> None:
        """Delete every key that starts with `prefix`."""
        ...


class CachingAuthorizer:
    """Caches another authorizer's answers per subject and action for `ttl` seconds.

    `ttl` is the revocation bound: a permission revoked at the source stops granting access at
    most `ttl` seconds later. `maxsize` caps the number of entries in this process, dropping the
    least recently used first. `shared` adds a cache all processes share (for example
    `RedisCache`), under keys starting with `namespace`; this process then keeps its own copy of
    each answer for at most `local_ttl` seconds, which bounds how long an `invalidate()` made in
    another process takes to reach this one. Errors and malformed answers are never cached.
    `clock` is for tests.

    Every `CachingAuthorizer` with the same `shared` cache and `namespace` uses the same answers,
    whatever authorizer it wraps: give each differently configured authorizer (another policy,
    store, tenant model or action mapping) a namespace of its own.
    """

    def __init__(
        self,
        authorizer: Authorizer,
        *,
        ttl: float = 30.0,
        maxsize: int = 100_000,
        shared: SharedCache | None = None,
        namespace: str = "needtoknow",
        local_ttl: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0 or maxsize < 1 or local_ttl <= 0:
            raise ValueError("ttl, local_ttl and maxsize must be positive")
        if not namespace:
            raise ValueError("namespace must be non-empty")
        self.authorizer = authorizer
        self.ttl = ttl
        self.maxsize = maxsize
        self.shared = shared
        self.namespace = namespace
        # Without a shared cache this process's copy is the only one, and it lives for ttl.
        self.local_ttl = ttl if shared is None else min(local_ttl, ttl)
        self._clock = clock
        # Shared entries carry wall-clock expiry times; a test clock stands in for both clocks.
        self._wall = time.time if clock is time.monotonic else clock
        self._entries: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        # Bumped by every invalidate(). An answer obtained by a call that started before the bump
        # may predate the change that prompted it, so it is not kept.
        self._generation = 0
        # The shared cache's global epoch and when this process read it; read again after local_ttl.
        self._global_epoch: tuple[float, str | None] | None = None
        # Bumped when invalidate() sets the global epoch, so a read already in flight cannot put
        # back the epoch it replaced.
        self._global_version = 0

    # ---- this process's cache --------------------------------------------------------------

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

    def _put(self, key: Any, value: Any, generation: int, ttl: float | None = None) -> None:
        lifetime = self.local_ttl if ttl is None else min(ttl, self.local_ttl)
        with self._lock:
            if generation != self._generation:
                return  # invalidate() ran while this answer was being obtained
            self._entries[key] = (self._clock() + lifetime, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    # ---- the shared cache ------------------------------------------------------------------

    def _tag(self, subject_id: Any) -> str:
        # The subject id's digest is a Redis hash tag, so the keys of every subject with that id,
        # and the id's epoch, share a cluster slot and are read together.
        return f"{self.namespace}:{{{_digest(str(subject_id))}}}"

    def _name(self, key: tuple[Any, ...]) -> str:
        kind, subject, action, *rest = key
        detail = _digest(kind, action, *(part for pair in rest for part in pair))
        return f"{self._tag(subject.id)}:{_subject_digest(subject)}:{detail}"

    def _epoch_name(self, subject_id: Any) -> str:
        # Never under "namespace:", the prefix that invalidate() deletes in every scope, so no
        # invalidation removes an epoch, not even one an invalidate() of an id has just written
        # while an invalidate() of everyone is still deleting. An id's epoch keeps the id's hash
        # tag, so it shares a cluster slot with the id's entries and is read with them.
        if subject_id is None:
            return f"{self.namespace}!epoch"
        return f"{self.namespace}!epoch:{{{_digest(str(subject_id))}}}"

    def _global(self) -> tuple[str | None, float]:
        """The shared cache's global epoch and when this process read it (on its own clock), read
        again once this process's copy is `local_ttl` old."""
        assert self.shared is not None
        now = self._clock()
        with self._lock:
            known, version = self._global_epoch, self._global_version
        if known is not None and now - known[0] < self.local_ttl:
            return known[1], known[0]
        (blob,) = self.shared.get_many([self._epoch_name(None)])
        epoch = _epoch_id(blob)
        with self._lock:
            if self._global_version != version and self._global_epoch is not None:
                # invalidate() set a new epoch while this read was in flight: it wins.
                return self._global_epoch[1], self._global_epoch[0]
            self._global_epoch = (now, epoch)
        return epoch, now

    def _encode(self, value: Any, expires: float, epoch: list[str | None]) -> bytes | None:
        if value is _NONE:
            payload: Any = None
        elif isinstance(value, bool):
            payload = value
        else:
            try:
                payload = {"filter": filter_to_dict(value)}
            except TypeError:
                return None  # not a filter: kept out of the shared cache
        entry = {"expires": expires, "epoch": epoch, "value": payload}
        return json.dumps(entry, separators=(",", ":")).encode()

    def _decode(
        self, key: tuple[Any, ...], blob: bytes | None, now: float, epoch: list[str | None]
    ) -> tuple[Any, float]:
        if blob is None:
            return _MISS, 0.0
        try:
            entry = json.loads(blob)
            expires, payload = float(entry["expires"]), entry["value"]
            if entry["epoch"] != epoch:
                return _MISS, 0.0  # written before an invalidate(), perhaps by a call still in flight
        except (ValueError, TypeError, KeyError, RecursionError):  # nested too deep to decode: a miss
            return _MISS, 0.0
        remaining = min(expires - now, self.ttl)
        if remaining <= 0:
            return _MISS, 0.0
        if key[0] == "check":
            return (payload, remaining) if isinstance(payload, bool) else (_MISS, 0.0)
        if payload is None:
            return _NONE, remaining
        try:
            return filter_from_dict(payload["filter"]), remaining
        except (ValueError, TypeError, KeyError):
            return _MISS, 0.0

    def _shared_get(self, keys: list[tuple[Any, ...]], generation: int) -> tuple[list[Any], Any]:
        """Answers for these keys, all for one subject, from the shared cache (None for each miss),
        copied into this process's cache for the time they have left; and the epochs read with
        them, under which answers learned now are written (`_UNKNOWN` if the read failed)."""
        if self.shared is None or not keys:
            return [None] * len(keys), _UNKNOWN
        try:
            global_epoch, read_at = self._global()
            epoch: list[str | None] = [global_epoch, None]
            names = [self._name(k) for k in keys]
            blobs = self.shared.get_many([*names, self._epoch_name(keys[0][1].id)])
            if len(blobs) != len(names) + 1:
                raise ValueError(f"{len(blobs)} values for {len(names) + 1} keys")
            epoch[1] = _epoch_id(blobs[-1])
        except Exception:
            _log.warning("needtoknow shared cache read failed; asking the authorizer", exc_info=True)
            return [None] * len(keys), _UNKNOWN
        now = self._wall()
        # An entry is accepted against this process's copy of the global epoch, which is up to
        # local_ttl old. Its copy here expires when that epoch would be read again, so an
        # invalidate() of everyone made elsewhere reaches this process within local_ttl, not twice it.
        fresh_for = read_at + self.local_ttl - self._clock()
        found: list[Any] = []
        for key, blob in zip(keys, blobs[:-1], strict=True):
            value, remaining = self._decode(key, blob, now, epoch)
            if value is _MISS:
                found.append(None)
            else:
                self._put(key, value, generation, min(remaining, fresh_for))
                found.append(value)
        return found, epoch

    def _shared_put(self, entries: Iterable[tuple[tuple[Any, ...], Any]], epoch: Any) -> None:
        if self.shared is None or epoch is _UNKNOWN:
            return
        expires = self._wall() + self.ttl
        blobs = {}
        for key, value in entries:
            blob = self._encode(value, expires, epoch)
            if blob is not None:
                blobs[self._name(key)] = blob
        if not blobs:
            return
        try:
            self.shared.set_many(blobs, self.ttl)
        except Exception:
            _log.warning("needtoknow shared cache write failed", exc_info=True)

    def invalidate(self, subject: Subject | str | None = None) -> None:
        """Drop every cached decision, or those for one subject id, here and in the shared cache.

        `subject` is a `Subject` or its id; the answers for every subject with that id are dropped,
        whatever groups and attributes they came with. An answer the authorizer gave before this
        call, to a retrieval still in progress, is not cached afterwards, here or in the shared
        cache. Other processes stop using their own copies within `local_ttl` seconds. A failure
        to reach the shared cache is raised: the caller asked for the revocation now.
        """
        if subject is not None and not isinstance(subject, (Subject, str)):
            # An id of another type (42 for "42") would miss this process's entries, keyed by the id.
            raise TypeError(f"invalidate() takes a Subject, a subject id or None, not {type(subject).__name__}")
        subject_id = subject.id if isinstance(subject, Subject) else subject
        if subject_id == "":
            raise ValueError("a subject id is non-empty; pass None to drop every decision")
        self._drop_local(subject_id)  # first, so this process stops at once even if Redis fails below
        if self.shared is None:
            return
        # A new epoch first, so that entries written under the old one are refused from now on;
        # then the entries themselves are deleted. Epochs lie outside "namespace:", so they stay.
        # Hex, not raw random bytes: a Redis client made with decode_responses=True must read it.
        epoch = secrets.token_hex(16).encode()
        lifetime = self.ttl + _EPOCH_MARGIN
        if subject_id is None:
            self.shared.set_many({self._epoch_name(None): epoch}, lifetime)
            with self._lock:
                self._global_version += 1
                self._global_epoch = (self._clock(), _epoch_id(epoch))
            self.shared.delete_prefix(f"{self.namespace}:")
        else:
            self.shared.set_many({self._epoch_name(subject_id): epoch}, lifetime)
            self.shared.delete_prefix(f"{self._tag(subject_id)}:")
        # Again: a retrieval here may have copied an old shared entry into this process after the
        # first drop and before the new epoch, under the generation that drop started.
        self._drop_local(subject_id)

    def _drop_local(self, subject_id: Any) -> None:
        """Drop this process's answers for `subject_id` (all of them for None), and refuse answers
        obtained by calls that began before now."""
        with self._lock:
            self._generation += 1
            if subject_id is None:
                self._entries.clear()
            else:
                for key in [k for k in self._entries if k[1].id == subject_id]:
                    del self._entries[key]

    # ---- Authorizer ------------------------------------------------------------------------

    def _lookup(self, key: tuple[Any, ...], generation: int) -> tuple[Any, Any]:
        """The cached answer for `key` or None, and the shared epochs to write a new answer under."""
        cached = self._get(key)
        if cached is not None:
            return cached, _UNKNOWN
        found, epoch = self._shared_get([key], generation)
        return found[0], epoch

    def _remember(self, key: tuple[Any, ...], result: Filter | None, generation: int, epoch: Any) -> Filter | None:
        value = _NONE if result is None else result
        self._put(key, value, generation)
        self._shared_put([(key, value)], epoch)
        return result

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        key, generation = ("filter", subject, action), self._generation
        cached, epoch = self._lookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        return self._remember(key, self.authorizer.filter_for(subject, action), generation, epoch)

    def scope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        """The wrapped authorizer's `scope_for`, cached like `filter_for` (None if it has none)."""
        inner = getattr(self.authorizer, "scope_for", None)
        if inner is None:
            return None
        key, generation = ("scope", subject, action), self._generation
        cached, epoch = self._lookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        return self._remember(key, inner(subject, action), generation, epoch)

    async def _alookup(self, key: tuple[Any, ...], generation: int) -> tuple[Any, Any]:
        cached = self._get(key)
        if cached is not None or self.shared is None:
            return cached, _UNKNOWN
        found, epoch = await asyncio.to_thread(self._shared_get, [key], generation)
        return found[0], epoch

    async def _aremember(
        self, key: tuple[Any, ...], result: Filter | None, generation: int, epoch: Any
    ) -> Filter | None:
        if self.shared is None:
            return self._remember(key, result, generation, epoch)
        return await asyncio.to_thread(self._remember, key, result, generation, epoch)

    async def ascope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        if not hasattr(self.authorizer, "scope_for") and not hasattr(self.authorizer, "ascope_for"):
            return None
        key, generation = ("scope", subject, action), self._generation
        cached, epoch = await self._alookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        native = getattr(self.authorizer, "ascope_for", None)
        result: Filter | None
        if native is not None:
            result = await native(subject, action)
        else:
            result = await asyncio.to_thread(self.authorizer.scope_for, subject, action)  # type: ignore[attr-defined]
        return await self._aremember(key, result, generation, epoch)

    def _local(self, subject: Subject, hits: Sequence[Hit], action: str) -> tuple[list[Any], list[bool | None]]:
        keys = [("check", subject, action, _hit_key(h)) for h in hits]
        return keys, [self._get(k) for k in keys]

    @staticmethod
    def _fill(cached: list[bool | None], found: list[Any]) -> None:
        missing = [i for i, known in enumerate(cached) if known is None]
        for i, value in zip(missing, found, strict=True):
            cached[i] = value

    def _merge(
        self, keys: list[Any], cached: list[bool | None], fresh: Sequence[bool], generation: int
    ) -> tuple[list[bool], list[tuple[Any, bool]]]:
        fresh = list(fresh)
        missing = sum(known is None for known in cached)
        if len(fresh) != missing:
            # Fail closed here: answers for the uncached hits that do not line up with them could,
            # merged with the cached ones, still have the right total length for the retriever.
            raise AuthorizationError(f"authorizer returned {len(fresh)} decisions for {missing} hits")
        answers = iter(fresh)
        decisions: list[bool] = []
        learned: list[tuple[Any, bool]] = []
        for key, known in zip(keys, cached, strict=True):
            if known is None:
                known = next(answers)
                if isinstance(known, bool):  # never cache a malformed answer; the retriever rejects it
                    self._put(key, known, generation)
                    learned.append((key, known))
            decisions.append(known)
        return decisions, learned

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        generation = self._generation
        keys, cached = self._local(subject, hits, action)
        epoch: Any = _UNKNOWN
        if self.shared is not None and None in cached:
            missing = [k for k, known in zip(keys, cached, strict=True) if known is None]
            found, epoch = self._shared_get(missing, generation)
            self._fill(cached, found)
        todo = [h for h, known in zip(hits, cached, strict=True) if known is None]
        fresh = self.authorizer.check(subject, todo, action) if todo else []
        decisions, learned = self._merge(keys, cached, fresh, generation)
        self._shared_put(learned, epoch)
        return decisions

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        key, generation = ("filter", subject, action), self._generation
        cached, epoch = await self._alookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        inner = getattr(self.authorizer, "afilter_for", None)
        result: Filter | None
        if inner is not None:
            result = await inner(subject, action)
        else:
            result = await asyncio.to_thread(self.authorizer.filter_for, subject, action)
        return await self._aremember(key, result, generation, epoch)

    async def acheck(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        generation = self._generation
        keys, cached = self._local(subject, hits, action)
        epoch: Any = _UNKNOWN
        if self.shared is not None and None in cached:
            todo_keys = [k for k, known in zip(keys, cached, strict=True) if known is None]
            found, epoch = await asyncio.to_thread(self._shared_get, todo_keys, generation)
            self._fill(cached, found)
        todo = [h for h, known in zip(hits, cached, strict=True) if known is None]
        fresh: Sequence[bool] = []
        if todo:
            inner = getattr(self.authorizer, "acheck", None)
            if inner is not None:
                fresh = await inner(subject, todo, action)
            else:
                fresh = await asyncio.to_thread(self.authorizer.check, subject, todo, action)
        decisions, learned = self._merge(keys, cached, fresh, generation)
        if learned and epoch is not _UNKNOWN:
            await asyncio.to_thread(self._shared_put, learned, epoch)
        return decisions


# ---- Redis ---------------------------------------------------------------------------------------

_SIGNATURE = 64  # hex characters of an HMAC-SHA256


def _glob_escape(text: str) -> str:
    return "".join("\\" + c if c in "*?[]\\" else c for c in text)


class RedisCache:
    """A `SharedCache` on Redis or a Redis-compatible store (Valkey, Dragonfly), with redis-py.

    Install with `pip install "needtoknow[redis]"` and pass a `redis.Redis` (or `RedisCluster`)
    client. Entries expire in Redis after the cache's `ttl`.

    Anyone who can write to this Redis could otherwise plant a decision. Pass `signing_key`
    (random bytes known only to your application, for example 32 from `secrets.token_bytes`) and
    every value is signed with HMAC-SHA256 over its key and content: an entry without a valid
    signature is ignored, so the authorizer is asked instead.
    """

    def __init__(self, client: Any, *, signing_key: bytes | None = None) -> None:
        if signing_key is not None and len(signing_key) < 16:
            raise ValueError("signing_key should be at least 16 random bytes")
        self.client = client
        self._key = signing_key

    def _signature(self, key: str, value: bytes) -> bytes:
        assert self._key is not None
        return hmac.new(self._key, key.encode() + b"\x00" + value, hashlib.sha256).hexdigest().encode()

    def get_many(self, keys: Sequence[str]) -> list[bytes | None]:
        values: list[bytes | None] = []
        for key, raw in zip(keys, self.client.mget(list(keys)), strict=True):
            blob = raw.encode() if isinstance(raw, str) else raw
            if blob is not None and self._key is not None:
                signature, blob = blob[:_SIGNATURE], blob[_SIGNATURE:]
                if not hmac.compare_digest(signature, self._signature(key, blob)):
                    _log.warning("needtoknow shared cache entry %s has no valid signature; ignored", key)
                    blob = None
            values.append(blob)
        return values

    def set_many(self, entries: Mapping[str, bytes], ttl: float) -> None:
        pipe = self.client.pipeline(transaction=False)
        for key, value in entries.items():
            blob = self._signature(key, value) + value if self._key is not None else value
            pipe.set(key, blob, px=max(1, int(ttl * 1000)))
        pipe.execute()

    def delete_prefix(self, prefix: str) -> None:
        batch: list[Any] = []
        for key in self.client.scan_iter(match=_glob_escape(prefix) + "*", count=1000):
            batch.append(key)
            if len(batch) >= 500:
                self.client.unlink(*batch)
                batch = []
        if batch:
            self.client.unlink(*batch)
