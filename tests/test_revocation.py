"""Revocation through CachingAuthorizer: what `invalidate()` promises, under concurrent retrievals.

The promise (docs/operations.md): after `invalidate()` returns, no retrieval that starts in this
process uses an answer given before it, and other replicas sharing the cache stop within
`local_ttl`. The hard case is an answer the authorizer gave before the revocation, to a call that
is still in flight when `invalidate()` runs: it must not be cached afterwards, here or in the
shared cache.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from wardowl import (
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
)

BOB = Subject("bob", ["eng"])
ANN = Subject("ann", ["hr"])
HITS = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "t", 1.0, {"allowed_groups": ["hr"]})]


class Shared:
    """A dictionary standing in for Redis, safe to use from several threads; it ignores TTLs."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get_many(self, keys):
        with self._lock:
            return [self.data.get(k) for k in keys]

    def set_many(self, entries, ttl):
        with self._lock:
            self.data.update(entries)

    def delete_prefix(self, prefix):
        with self._lock:
            for key in [k for k in self.data if k.startswith(prefix)]:
                del self.data[key]


class Held:
    """MetadataACLAuthorizer whose answers wait until `release` is set, so a test can revoke
    while a call is in flight. Counts the hits and filters it was asked about."""

    def __init__(self):
        self.inner = MetadataACLAuthorizer()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.checks = self.filters = 0

    def _wait(self):
        self.entered.set()
        assert self.release.wait(5), "the test never released the authorizer"

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        self._wait()
        return self.inner.check(subject, hits, action)

    def filter_for(self, subject, action="read"):
        self.filters += 1
        self._wait()
        return self.inner.filter_for(subject, action)


def _in_flight(call, held, revoke):
    """Start `call` in a thread, revoke while the authorizer is answering it, then let it finish."""
    worker = threading.Thread(target=call)
    worker.start()
    assert held.entered.wait(5)
    revoke()
    held.release.set()
    worker.join(5)
    assert not worker.is_alive()


def _released():
    held = Held()
    held.release.set()
    return held


# ---- an answer given before invalidate() is not cached after it ------------------------------------


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_an_answer_given_before_invalidate_is_not_cached_in_this_process(scope):
    held = Held()
    cache = CachingAuthorizer(held, ttl=30)
    _in_flight(lambda: cache.check(BOB, HITS), held, lambda: cache.invalidate(BOB if scope == "subject" else None))
    cache.check(BOB, HITS)
    assert held.checks == 4  # asked again: the answer from before the revocation was not kept


def test_a_filter_given_before_invalidate_is_not_cached():
    held = Held()
    cache = CachingAuthorizer(held, ttl=30)
    _in_flight(lambda: cache.filter_for(BOB), held, lambda: cache.invalidate(BOB))
    cache.filter_for(BOB)
    assert held.filters == 2


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_an_answer_another_replica_writes_after_invalidate_is_never_read(scope):
    shared = Shared()
    held, fresh = Held(), _released()
    slow = CachingAuthorizer(held, shared=shared)  # its answer arrives after the revocation
    admin = CachingAuthorizer(MetadataACLAuthorizer(), shared=shared)  # the replica that revokes
    _in_flight(lambda: slow.check(BOB, HITS), held, lambda: admin.invalidate(BOB if scope == "subject" else None))
    assert any("!epoch" not in key for key in shared.data)  # the late answer was written...
    third = CachingAuthorizer(fresh, shared=shared)
    third.check(BOB, HITS)
    assert fresh.checks == 2  # ...but no replica uses it


@pytest.mark.parametrize("scope", ["subject", "everyone"])
def test_an_answer_copied_from_the_shared_cache_while_invalidate_runs_is_not_kept(scope):
    class Racing(Shared):
        """Runs `during` once, just before the next epoch is written: in the middle of invalidate()."""

        during = None

        def set_many(self, entries, ttl):
            if self.during is not None and any("!epoch" in k for k in entries):
                during, self.during = self.during, None
                during()
            super().set_many(entries, ttl)

    shared, held = Racing(), _released()
    cache = CachingAuthorizer(held, shared=shared)
    cache.check(BOB, HITS)  # cached in this process and in the shared cache
    # A retrieval in this process after it dropped its own entries, before the new epoch exists:
    # it finds the old entry in the shared cache, still valid, and copies it.
    shared.during = lambda: cache.check(BOB, HITS)
    cache.invalidate(BOB if scope == "subject" else None)
    asked = held.checks
    cache.check(BOB, HITS)
    assert held.checks == asked + 2  # asked again: the copy made during invalidate() was dropped


def test_a_shared_entry_put_back_after_invalidate_is_refused():
    shared = Shared()
    first = CachingAuthorizer(MetadataACLAuthorizer(), shared=shared)
    first.check(BOB, HITS)
    before = dict(shared.data)
    first.invalidate(BOB)
    shared.data.update(before)  # the entries as they were before the revocation, written back
    held = _released()
    CachingAuthorizer(held, shared=shared).check(BOB, HITS)
    assert held.checks == 2


def test_async_answers_given_before_invalidate_are_not_cached():
    class HeldAsync:
        def __init__(self):
            self.inner, self.checks = MetadataACLAuthorizer(), 0
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        def check(self, subject, hits, action="read"):
            return self.inner.check(subject, hits, action)

        def filter_for(self, subject, action="read"):
            return self.inner.filter_for(subject, action)

        async def acheck(self, subject, hits, action="read"):
            self.checks += len(hits)
            self.entered.set()
            await self.release.wait()
            return self.inner.check(subject, hits, action)

    async def scenario(shared):
        held = HeldAsync()
        cache = CachingAuthorizer(held, shared=shared)
        call = asyncio.create_task(cache.acheck(BOB, HITS))
        await held.entered.wait()
        cache.invalidate(BOB)
        held.release.set()
        await call
        await cache.acheck(BOB, HITS)
        return held.checks

    assert asyncio.run(scenario(None)) == 4
    assert asyncio.run(scenario(Shared())) == 4


# ---- invalidate() by subject id --------------------------------------------------------------------


BOBS = [Subject("bob", ["eng"], {"tenant": "acme"}), Subject("bob", ["hr"], {"tenant": "globex"})]


def test_invalidate_drops_every_subject_with_the_id_whatever_its_groups_and_attributes():
    held = _released()
    cache = CachingAuthorizer(held)
    for subject in [*BOBS, ANN]:
        cache.check(subject, HITS)
    cache.invalidate("bob")  # the id alone, as a revocation hook usually has it
    for subject in [*BOBS, ANN]:
        cache.check(subject, HITS)
    assert held.checks == 6 + 4  # asked again for both bobs, not for ann


def test_invalidate_by_id_reaches_the_shared_cache():
    shared, two = Shared(), _released()
    a, b = CachingAuthorizer(_released(), shared=shared), CachingAuthorizer(two, shared=shared)
    for subject in [*BOBS, ANN]:
        a.check(subject, HITS)
    a.invalidate(BOBS[0])  # a Subject works too: every subject with its id is dropped
    for subject in [*BOBS, ANN]:
        b.check(subject, HITS)
    assert two.checks == 4  # the other replica asked for both bobs; ann came from the shared cache


def test_invalidate_refuses_an_empty_id():
    with pytest.raises(ValueError):
        CachingAuthorizer(MetadataACLAuthorizer()).invalidate("")


# ---- revocation under concurrent retrievals ------------------------------------------------------


class Source:
    """The permission service: who may read each document, changed while retrievals run. Each
    answer takes a few milliseconds, so calls are in flight when a permission is revoked."""

    def __init__(self, readers):
        self.readers = {doc: set(users) for doc, users in readers.items()}
        self.lock = threading.Lock()

    def check(self, subject, hits, action):
        with self.lock:
            answer = [subject.id in self.readers.get(h.id, ()) for h in hits]
        time.sleep(0.003)
        return answer

    def revoke(self, doc, user):
        with self.lock:
            self.readers[doc].discard(user)


DOCS = [Document(f"doc-{i}", f"quarterly plan part {i}", {}) for i in range(6)]


def _traffic(retrievers, subject, stop, log):
    def user(retriever):
        while not stop.is_set():
            started = time.monotonic()
            ids = [h.id for h in retriever.retrieve("quarterly plan", subject, 6).hits]
            log.append((started, time.monotonic(), retriever, ids))

    threads = [threading.Thread(target=user, args=(r,)) for r in retrievers for _ in range(4)]
    for t in threads:
        t.start()
    return threads


@pytest.mark.parametrize("replicas", [1, 3])
def test_once_invalidate_returns_a_revoked_document_stops_being_returned(replicas):
    source = Source({d.id: {"bob"} for d in DOCS})
    shared = Shared() if replicas > 1 else None
    local_ttl = 0.1
    # Room for 4 of the 6 decisions a retrieval needs, so every retrieval asks the source about
    # some of them and answers are in flight when the permission is revoked; ttl is long, so an
    # answer kept by mistake would be served for the rest of the test.
    caches = [
        CachingAuthorizer(FunctionAuthorizer(source.check), ttl=30, maxsize=4, shared=shared, local_ttl=local_ttl)
        for _ in range(replicas)
    ]
    retrievers = [PermissionedRetriever(InMemoryBackend(DOCS), c, strategy="postfilter") for c in caches]
    bob, stop, log = Subject("bob"), threading.Event(), []
    threads = _traffic(retrievers, bob, stop, log)
    time.sleep(0.3)  # every replica has cached bob's access to every document
    source.revoke("doc-3", "bob")
    caches[0].invalidate(bob)  # the revocation hook runs on one replica
    revoked_at = time.monotonic()
    time.sleep(0.6 + local_ttl)
    stop.set()
    for t in threads:
        t.join(5)

    seen_after = [(started - revoked_at, r) for started, _, r, ids in log if started > revoked_at and "doc-3" in ids]
    # This process: nothing that started after invalidate() returned. Other replicas: they may use
    # their own copies for local_ttl; a late answer written to the shared cache is never read.
    slack = 0.25  # scheduling on a busy CI runner, between an answer and its being kept
    late = [delay for delay, r in seen_after if r is retrievers[0] or delay > local_ttl + slack]
    assert not late, f"doc-3 was returned {max(late):.3f} s after invalidate() returned"
    assert any(started > revoked_at + local_ttl + slack for started, *_ in log)  # traffic kept going
    assert all("doc-2" in ids for *_, ids in log)  # and the permissions not revoked still apply
