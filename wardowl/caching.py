"""CachingAuthorizer: fewer calls to the permission service, with a stated revocation bound.

Wrap any authorizer:

    authorizer = CachingAuthorizer(OpenFGAAuthorizer(...), ttl=30)

Revocation bound: a permission revoked in the source of truth stops granting access at most `ttl`
seconds later (plus whatever delay the permission service itself has). Call `invalidate()` after
a change you know about to drop cached decisions at once. An answer the authorizer gave before that
call, to a retrieval still in progress, is not cached after it either.

What is cached, per (subject, action): the pre-filter from `filter_for`, and each `check` decision,
keyed by the chunk id and its metadata, so a chunk whose metadata changes is decided again.
Errors and malformed answers are never cached: every failure reaches the caller and retrieval fails
closed. A subject or chunk holding a value the cache key cannot encode exactly is not cached either:
the authorizer is asked every time.

Across processes: pass `shared=RedisCache(redis_client)`, or any `SharedCache`, with a `namespace`
of its own for the authorizer, and every replica uses the answers any of them obtained. Each process
then keeps its own copy of an answer for at most `local_ttl` seconds (1 by default) before reading
the shared cache again, so `invalidate()` in one replica reaches every other one within
`local_ttl`. Each shared entry carries its expiry time, so the revocation bound holds however many
replicas copy it (with clocks kept in sync, as NTP does). `invalidate()` also replaces an epoch
stored in the shared cache, and an entry is used only under the epoch it was written in, so an
answer another replica obtained before the invalidation and writes after it is never read. A shared
cache that fails is treated as a miss: the authorizer is asked, and the cache is never the reason
access is granted.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from wardowl.authz import AuthorizationError, Authorizer
from wardowl.filters import Filter, filter_from_dict, filter_to_dict
from wardowl.types import Hit, Subject

__all__ = ["CacheStats", "CachingAuthorizer", "RedisCache", "SharedCache"]

_NONE = object()  # cached "this authorizer returns no filter"
_MISS = object()  # a shared entry that is absent, expired, unreadable, or from an earlier epoch
_UNKNOWN = object()  # the shared epochs could not be read, so nothing learned is written there
# How much longer than `ttl` an epoch is kept. Until it expires, entries written under the epoch it
# replaced are refused; by then they have long expired, so "no epoch yet" cannot bring one back.
_EPOCH_MARGIN = 3600.0
_log = logging.getLogger("wardowl")


# ---- cache keys ----------------------------------------------------------------------------------
# A key must tell apart every two subjects or chunks an authorizer could tell apart. Values are
# encoded with their type and length, so 1, "1", True and 1.0 differ, as do {1} and "{1}", a key 1
# and a key "1", and ("a", "b") and ("a\x00b",). A value of a type not listed has no exact encoding:
# the subject or chunk holding it is not cached.

_TIMES = (datetime.date, datetime.time, decimal.Decimal, uuid.UUID)


def _text(out: list[str], tag: str, text: str) -> None:
    out.append(f"{tag}{len(text)}:{text}")


def _encode(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("n")
    elif isinstance(value, bool):
        out.append("t" if value else "f")
    elif isinstance(value, int):
        _text(out, "i", int.__repr__(value))
    elif isinstance(value, float):
        _text(out, "d", float.__repr__(value))
    elif isinstance(value, str):
        _text(out, "s", str.__str__(value))
    elif isinstance(value, (bytes, bytearray)):
        _text(out, "b", bytes(value).hex())
    elif isinstance(value, Mapping):
        pairs = sorted((_canonical(k), _canonical(v)) for k, v in value.items())
        out.append(f"m{len(pairs)}:")
        out.extend(part for pair in pairs for part in pair)
    elif isinstance(value, (list, tuple)):
        out.append(f"{'l' if isinstance(value, list) else 'u'}{len(value)}:")
        for item in value:
            _encode(item, out)
    elif isinstance(value, (set, frozenset)):
        items = sorted(_canonical(item) for item in value)
        out.append(f"e{len(items)}:")
        out.extend(items)
    elif isinstance(value, _TIMES):
        _text(out, "x", type(value).__qualname__)
        _text(out, "", value.isoformat() if isinstance(value, (datetime.date, datetime.time)) else str(value))
    else:
        raise TypeError(f"a {type(value).__name__} has no exact encoding in a cache key")


def _canonical(value: Any) -> str:
    """`value` as text that no value of another type, or with other contents, shares."""
    out: list[str] = []
    _encode(value, out)
    return "".join(out)


def _digest(*parts: str) -> str:
    """A digest of `parts`, each prefixed with its length, so no other list of parts has the same one."""
    h = hashlib.blake2b(digest_size=16)
    for part in parts:
        data = part.encode("utf-8", "surrogatepass")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _key_of(value: Any) -> str | None:
    """The digest of `value`'s exact encoding, or None when it holds a value that has none."""
    try:
        return _digest(_canonical(value))
    except (TypeError, RecursionError):  # an unknown type, or nested too deep to encode
        return None


def _subject_key(subject: Subject) -> str | None:
    return _key_of((subject.id, subject.groups, subject.attributes))


def _hit_key(hit: Hit) -> str | None:
    return _key_of((hit.id, hit.metadata))


def _cacheable(answer: Any) -> bool:
    """True for None and a well-formed filter; anything else is a malformed answer, never cached."""
    if answer is None:
        return True
    try:
        filter_to_dict(answer)
    except (TypeError, RecursionError):
        return False
    return True


# ---- which authorizer a shared namespace answers for, in this process ----------------------------

_claims_lock = threading.Lock()
# Per shared store (a SharedCache, or the Redis client a RedisCache wraps), by id: a weak reference
# to it, and the authorizer type each namespace answers for. The reference tells a store from a later
# one that was given the same id after the first was gone.
_claims: dict[int, tuple[weakref.ref[Any], dict[str, str]]] = {}


def _claim(shared: SharedCache, namespace: str, authorizer: Authorizer) -> None:
    """Refuse a second kind of authorizer under a shared store and namespace this process uses."""
    store = shared.client if isinstance(shared, RedisCache) else shared
    kind = f"{type(authorizer).__module__}.{type(authorizer).__qualname__}"
    with _claims_lock:
        for gone in [key for key, (ref, _) in _claims.items() if ref() is None]:
            del _claims[gone]
        entry = _claims.get(id(store))
        if entry is None or entry[0]() is not store:
            try:
                entry = (weakref.ref(store), {})
            except TypeError:  # no weak references to it: the explicit namespace is the only guard
                return
            _claims[id(store)] = entry
        claimed = entry[1].setdefault(namespace, kind)
    if claimed != kind:
        raise ValueError(
            f"namespace {namespace!r} of this shared cache already holds the answers of a {claimed}; a "
            f"{kind} would use them as its own. Give each differently configured authorizer its own namespace"
        )


def _epoch_id(blob: Any) -> str | None:
    """The epoch a shared cache holds, as text to compare; None when it holds none yet."""
    if blob is None:
        return None
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"a shared cache returned {type(blob).__name__}, not bytes")
    return bytes(blob).hex()


@dataclass(frozen=True)
class CacheStats:
    """What a `CachingAuthorizer` has done since it was created, from `stats()`, for monitoring."""

    hits: int
    """Answers served from this process's own cache."""
    shared_hits: int
    """Answers served from the shared cache."""
    misses: int
    """Answers the wrapped authorizer was asked for."""
    shared_failures: int
    """Reads and writes of the shared cache that failed; each failed read was treated as a miss."""
    entries: int
    """Answers this process holds now."""


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
    `RedisCache`), under keys starting with `namespace`, which it requires; this process then keeps
    its own copy of each answer for at most `local_ttl` seconds, which bounds how long an
    `invalidate()` made in another process takes to reach this one. Errors and malformed answers
    are never cached. `clock` is for tests.

    Every `CachingAuthorizer` with the same `shared` cache and `namespace` uses the same answers,
    whatever authorizer it wraps: give each differently configured authorizer (another policy,
    store, tenant model or action mapping) a namespace of its own. In one process, a second
    `CachingAuthorizer` that wraps an authorizer of another type under a shared cache and namespace
    already in use raises `ValueError`; it cannot tell two configurations of one type apart, nor
    see other processes.
    """

    def __init__(
        self,
        authorizer: Authorizer,
        *,
        ttl: float = 30.0,
        maxsize: int = 100_000,
        shared: SharedCache | None = None,
        namespace: str | None = None,
        local_ttl: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (math.isfinite(ttl) and math.isfinite(local_ttl)) or ttl <= 0 or maxsize < 1 or local_ttl <= 0:
            raise ValueError("ttl and local_ttl must be positive finite numbers, and maxsize positive")
        if namespace is not None and not namespace:
            raise ValueError("namespace must be non-empty")
        if shared is not None:
            if namespace is None:
                # The default namespace once shared by every authorizer: one answered for another.
                raise ValueError(
                    "a shared cache needs a namespace naming this authorizer's policy, for example"
                    " namespace='openfga-docs': caches sharing a namespace use each other's answers"
                )
            _claim(shared, namespace, authorizer)
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
        self._stats = {"hits": 0, "shared_hits": 0, "misses": 0, "shared_failures": 0}

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
            self._stats["hits"] += 1
            return value

    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._stats[name] += n

    def stats(self) -> CacheStats:
        """What this cache has done since it was created, and how many answers it holds now."""
        with self._lock:
            return CacheStats(**self._stats, entries=len(self._entries))

    def _put(self, key: Any, value: Any, generation: int, ttl: float | None = None) -> None:
        lifetime = self.local_ttl if ttl is None else min(ttl, self.local_ttl)
        if not (math.isfinite(lifetime) and lifetime > 0):
            return  # an entry that would never expire, or has already expired, is not kept
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
        # A key is (kind, subject, the subject's digest, action), and for a check the chunk's digest.
        kind, subject, who, *rest = key
        return f"{self._tag(subject.id)}:{who}:{_digest(kind, *rest)}"

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
        if not math.isfinite(expires):  # JSON can carry NaN and Infinity, and NaN never compares as expired
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
            _log.warning("wardowl shared cache read failed; asking the authorizer", exc_info=True)
            self._count("shared_failures")
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
        self._count("shared_hits", sum(value is not None for value in found))
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
            _log.warning("wardowl shared cache write failed", exc_info=True)
            self._count("shared_failures")

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

    @staticmethod
    def _answer_key(kind: str, subject: Subject, action: str) -> tuple[Any, ...] | None:
        """The key of a filter or scope answer, or None when the subject cannot be encoded exactly."""
        who = _subject_key(subject)
        return None if who is None else (kind, subject, who, action)

    def _lookup(self, key: tuple[Any, ...] | None, generation: int) -> tuple[Any, Any]:
        """The cached answer for `key` or None, and the shared epochs to write a new answer under."""
        if key is None:
            return None, _UNKNOWN
        cached = self._get(key)
        if cached is not None:
            return cached, _UNKNOWN
        found, epoch = self._shared_get([key], generation)
        return found[0], epoch

    def _remember(
        self, key: tuple[Any, ...] | None, result: Filter | None, generation: int, epoch: Any
    ) -> Filter | None:
        if key is None or not _cacheable(result):  # a malformed answer is returned, for the retriever to refuse
            return result
        value = _NONE if result is None else result
        self._put(key, value, generation)
        self._shared_put([(key, value)], epoch)
        return result

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        key, generation = self._answer_key("filter", subject, action), self._generation
        cached, epoch = self._lookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        self._count("misses")
        return self._remember(key, self.authorizer.filter_for(subject, action), generation, epoch)

    def scope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        """The wrapped authorizer's `scope_for`, cached like `filter_for` (None if it has none)."""
        inner = getattr(self.authorizer, "scope_for", None)
        if inner is None:
            return None
        key, generation = self._answer_key("scope", subject, action), self._generation
        cached, epoch = self._lookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        self._count("misses")
        return self._remember(key, inner(subject, action), generation, epoch)

    async def _alookup(self, key: tuple[Any, ...] | None, generation: int) -> tuple[Any, Any]:
        if key is None:
            return None, _UNKNOWN
        cached = self._get(key)
        if cached is not None or self.shared is None:
            return cached, _UNKNOWN
        found, epoch = await asyncio.to_thread(self._shared_get, [key], generation)
        return found[0], epoch

    async def _aremember(
        self, key: tuple[Any, ...] | None, result: Filter | None, generation: int, epoch: Any
    ) -> Filter | None:
        if self.shared is None or key is None:
            return self._remember(key, result, generation, epoch)
        return await asyncio.to_thread(self._remember, key, result, generation, epoch)

    async def ascope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        if not hasattr(self.authorizer, "scope_for") and not hasattr(self.authorizer, "ascope_for"):
            return None
        key, generation = self._answer_key("scope", subject, action), self._generation
        cached, epoch = await self._alookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        self._count("misses")
        native = getattr(self.authorizer, "ascope_for", None)
        result: Filter | None
        if native is not None:
            result = await native(subject, action)
        else:
            result = await asyncio.to_thread(self.authorizer.scope_for, subject, action)  # type: ignore[attr-defined]
        return await self._aremember(key, result, generation, epoch)

    def _local(
        self, subject: Subject, hits: Sequence[Hit], action: str
    ) -> tuple[list[tuple[Any, ...] | None], list[bool | None]]:
        """Each hit's key (None for one that cannot be encoded exactly) and its cached decision."""
        who = _subject_key(subject)
        keys: list[tuple[Any, ...] | None] = []
        for hit in hits:
            chunk = None if who is None else _hit_key(hit)
            keys.append(None if chunk is None else ("check", subject, who, action, chunk))
        return keys, [None if key is None else self._get(key) for key in keys]

    @staticmethod
    def _missing(
        keys: list[tuple[Any, ...] | None], cached: list[bool | None]
    ) -> tuple[list[int], list[tuple[Any, ...]]]:
        """The hits to look for in the shared cache, those not cached here that have a key: their
        positions, and their keys."""
        positions: list[int] = []
        wanted: list[tuple[Any, ...]] = []
        for i, (key, known) in enumerate(zip(keys, cached, strict=True)):
            if known is None and key is not None:
                positions.append(i)
                wanted.append(key)
        return positions, wanted

    @staticmethod
    def _fill(cached: list[bool | None], positions: list[int], found: list[Any]) -> None:
        for i, value in zip(positions, found, strict=True):
            cached[i] = value

    def _merge(
        self, keys: list[tuple[Any, ...] | None], cached: list[bool | None], fresh: Sequence[bool], generation: int
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
                # Never cache a malformed answer (the retriever rejects it), nor one without a key.
                if key is not None and isinstance(known, bool):
                    self._put(key, known, generation)
                    learned.append((key, known))
            decisions.append(known)
        return decisions, learned

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        generation = self._generation
        keys, cached = self._local(subject, hits, action)
        epoch: Any = _UNKNOWN
        positions, wanted = self._missing(keys, cached)
        if self.shared is not None and wanted:
            found, epoch = self._shared_get(wanted, generation)
            self._fill(cached, positions, found)
        todo = [h for h, known in zip(hits, cached, strict=True) if known is None]
        self._count("misses", len(todo))
        fresh = self.authorizer.check(subject, todo, action) if todo else []
        decisions, learned = self._merge(keys, cached, fresh, generation)
        self._shared_put(learned, epoch)
        return decisions

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        key, generation = self._answer_key("filter", subject, action), self._generation
        cached, epoch = await self._alookup(key, generation)
        if cached is not None:
            return None if cached is _NONE else cached
        self._count("misses")
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
        positions, wanted = self._missing(keys, cached)
        if self.shared is not None and wanted:
            found, epoch = await asyncio.to_thread(self._shared_get, wanted, generation)
            self._fill(cached, positions, found)
        todo = [h for h, known in zip(hits, cached, strict=True) if known is None]
        fresh: Sequence[bool] = []
        if todo:
            self._count("misses", len(todo))
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

    Install with `pip install "wardowl[redis]"` and pass a `redis.Redis` (or `RedisCluster`)
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
                    _log.warning("wardowl shared cache entry %s has no valid signature; ignored", key)
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
