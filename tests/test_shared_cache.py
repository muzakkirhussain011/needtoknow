"""CachingAuthorizer with a cache shared by several processes, and RedisCache against a real Redis."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import CachingAuthorizer, Hit, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.caching import RedisCache, SharedCache

BOB = Subject("bob", ["eng"])
HITS = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "t", 1.0, {"allowed_groups": ["hr"]})]


class Counting:
    def __init__(self):
        self.inner = MetadataACLAuthorizer()
        self.checks = self.filters = 0

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        return self.inner.check(subject, hits, action)

    def filter_for(self, subject, action="read"):
        self.filters += 1
        return self.inner.filter_for(subject, action)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeShared:
    """A dictionary standing in for Redis; it ignores TTLs, so expiry is the cache's own doing."""

    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.fail = False

    def get_many(self, keys):
        if self.fail:
            raise ConnectionError("down")
        return [self.data.get(k) for k in keys]

    def set_many(self, entries, ttl):
        if self.fail:
            raise ConnectionError("down")
        self.data.update(entries)

    def delete_prefix(self, prefix):
        if self.fail:
            raise ConnectionError("down")
        for key in [k for k in self.data if k.startswith(prefix)]:
            del self.data[key]


def _replicas(shared, clock=None, ttl=30.0):
    clock = clock or Clock()
    inners = [Counting(), Counting()]
    return clock, inners, [CachingAuthorizer(i, ttl=ttl, shared=shared, clock=clock) for i in inners]


def test_fake_shared_is_a_shared_cache():
    assert isinstance(FakeShared(), SharedCache)


def test_a_second_replica_uses_the_first_ones_answers():
    shared = FakeShared()
    _, (one, two), (a, b) = _replicas(shared)
    assert a.check(BOB, HITS) == [True, False] and a.filter_for(BOB) is not None
    assert b.check(BOB, HITS) == [True, False] and b.filter_for(BOB) == a.filter_for(BOB)
    assert (one.checks, one.filters) == (2, 1) and (two.checks, two.filters) == (0, 0)
    # Key names reveal neither the subject nor the chunks.
    assert not any("bob" in k or "allowed_groups" in k for k in shared.data)


def test_the_revocation_bound_holds_across_replicas():
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)  # written at t=1000, expires at t=1030
    clock.now += 25
    b.check(BOB, HITS)  # copied into b's own cache with 5 seconds left, not a fresh 30
    clock.now += 6
    b.check(BOB, HITS)
    assert two.checks == 2  # asked again: the shared entry and b's copy have both expired


def test_a_copied_entry_never_outlives_the_ttl_whatever_expiry_it_carries():
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS[:1])
    (key,) = shared.data
    entry = json.loads(shared.data[key])
    shared.data[key] = json.dumps({**entry, "expires": entry["expires"] + 10**9}).encode()
    b.check(BOB, HITS[:1])  # b copies it, for at most ttl
    shared.data.clear()  # (Redis drops it after the writer's ttl; signing_key rejects planted ones)
    clock.now += 31
    b.check(BOB, HITS[:1])
    assert two.checks == 1  # b's copy expired after ttl, so b asked again


def test_invalidate_reaches_every_replica():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    a.check(Subject("ann", ["hr"]), HITS)
    a.invalidate(BOB)
    b.check(BOB, HITS)
    b.check(Subject("ann", ["hr"]), HITS)
    assert two.checks == 2  # bob was asked again, ann still came from the shared cache
    a.invalidate()
    # Every entry is gone; the epochs stay (everyone's and bob's), and refuse late writes.
    assert len(shared.data) == 2 and all(key.startswith("needtoknow!epoch") for key in shared.data)


def test_invalidate_reaches_a_replica_holding_its_own_copy_within_local_ttl():
    shared = FakeShared()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    b.check(BOB, HITS)  # b now holds its own copy, for at most local_ttl (1 second)
    a.invalidate(BOB)
    clock.now += 0.5
    b.check(BOB, HITS)
    assert two.checks == 0  # still b's own copy
    clock.now += 0.6
    b.check(BOB, HITS)
    assert two.checks == 2  # its copy expired and the shared entry is gone, so b asked


def test_invalidating_everyone_reaches_a_replica_within_local_ttl_even_before_the_entries_are_deleted():
    class Lagging(FakeShared):
        """Deletes nothing: Redis's SCAN has not reached the entries yet."""

        def delete_prefix(self, prefix):
            pass

    shared = Lagging()
    clock, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    b.check(Subject("ann", ["hr"]), HITS)  # b reads the global epoch now, at t=1000
    asked = two.checks
    clock.now += 0.9
    a.invalidate()
    b.check(BOB, HITS)  # b's epoch is 0.9 s old, so it still accepts bob's entry, and copies it
    assert two.checks == asked
    clock.now += 0.6
    b.check(BOB, HITS)
    assert two.checks == asked + 2  # its copy expired with its epoch, 1 s after reading it, not 1.9 s


def test_a_global_epoch_read_in_flight_cannot_put_back_the_epoch_invalidate_replaced():
    class Racing(FakeShared):
        during = None

        def get_many(self, keys):
            values = super().get_many(keys)
            if self.during is not None and keys == ["needtoknow!epoch"]:
                during, self.during = self.during, None
                during()  # invalidate() runs after the old epoch was read, before it is kept
            return values

    shared = Racing()
    clock, (one, _two), (a, b) = _replicas(shared)
    a.invalidate()
    b.check(BOB, HITS)  # written under that epoch
    old = {key: value for key, value in shared.data.items() if "!epoch" not in key}
    clock.now += 2  # a's copy of the global epoch is old, so its next lookup reads it again
    shared.during = a.invalidate
    a.check(BOB, HITS)
    shared.data.update(old)  # answers from before the second invalidate(), put back
    asked = one.checks
    a.check(BOB, HITS)
    assert one.checks == asked + 2  # a kept the new epoch, so it refused the old answers


def test_invalidating_everyone_never_deletes_the_epoch_of_an_id():
    shared = FakeShared()
    _, _, (a, _b) = _replicas(shared)
    a.invalidate(BOB)
    bobs = {key: value for key, value in shared.data.items() if key != "needtoknow!epoch"}
    a.invalidate()  # while it deletes, an invalidate() of bob may have just written bob's epoch
    assert bobs and bobs.items() <= shared.data.items()


def test_epochs_are_text_that_a_redis_client_decoding_responses_can_read():
    shared = FakeShared()
    _, _, (a, _b) = _replicas(shared)
    a.invalidate(BOB)
    a.invalidate()
    assert all(value.decode("ascii").isalnum() for value in shared.data.values())  # only epochs are left


def test_invalidate_refuses_an_id_of_another_type():
    with pytest.raises(TypeError):
        CachingAuthorizer(Counting(), shared=FakeShared()).invalidate(42)  # the id "42" would be missed here


def test_without_a_shared_cache_entries_live_for_ttl():
    clock, inner = Clock(), Counting()
    cache = CachingAuthorizer(inner, ttl=30, local_ttl=1, clock=clock)
    cache.check(BOB, HITS)
    clock.now += 29
    cache.check(BOB, HITS)
    assert inner.checks == 2  # local_ttl only applies next to a shared cache


@pytest.mark.parametrize("options", [{"ttl": 0}, {"local_ttl": 0}, {"maxsize": 0}, {"namespace": ""}])
def test_invalid_options_are_refused(options):
    with pytest.raises(ValueError):
        CachingAuthorizer(Counting(), shared=FakeShared(), **options)


@pytest.mark.parametrize(
    "blob",
    [
        b"not json",
        b"{}",
        b'{"expires": "soon", "epoch": [null, null], "value": true}',
        b'{"expires": 1e12, "epoch": [null, null], "value": "yes"}',
        b'{"expires": 1e12, "value": true}',  # no epoch: written by an older version, or planted
        # Nested too deep for the JSON decoder. A short id: pytest puts the test id in an environment
        # variable, and Windows refuses one longer than 32767 characters.
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested-too-deep"),
    ],
)
def test_unreadable_entries_are_misses(blob):
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.check(BOB, HITS[:1])
    shared.data = dict.fromkeys(shared.data, blob)
    assert b.check(BOB, HITS[:1]) == [True] and two.checks == 1


def test_a_malformed_shared_filter_is_a_miss():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.filter_for(BOB)
    blob = b'{"expires": 1e12, "epoch": [null, null], "value": {"filter": {"in": "nonsense"}}}'
    shared.data = dict.fromkeys(shared.data, blob)
    assert b.filter_for(BOB) is not None and two.filters == 1


def test_a_failing_shared_cache_never_changes_an_answer():
    shared = FakeShared()
    _, (one, _two), (a, _b) = _replicas(shared)
    shared.fail = True
    assert a.check(BOB, HITS) == [True, False] and a.filter_for(BOB) is not None
    assert one.checks == 2
    with pytest.raises(ConnectionError):
        a.invalidate(BOB)  # the caller asked for a revocation now: its failure is reported


def test_async_paths_use_the_shared_cache():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    asyncio.run(a.acheck(BOB, HITS))
    asyncio.run(a.afilter_for(BOB))
    assert asyncio.run(b.acheck(BOB, HITS)) == [True, False]
    assert asyncio.run(b.afilter_for(BOB)) == a.filter_for(BOB)
    assert (two.checks, two.filters) == (0, 0)


@settings(max_examples=80, deadline=None)
@given(corpora(max_size=15), subjects, queries, st.integers(1, 5), st.sampled_from(["auto", "prefilter", "postfilter"]))
def test_sharing_never_changes_the_answer(docs, subject, query, k, strategy):
    shared = FakeShared()
    for _ in range(2):  # the second replica is served from the shared cache
        authz = CachingAuthorizer(MetadataACLAuthorizer(), shared=shared)
        retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + 1)
        assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


# ---- RedisCache against a real Redis -------------------------------------------------------

REDIS_URL = os.environ.get("NEEDTOKNOW_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="set NEEDTOKNOW_TEST_REDIS_URL to run against Redis")


def _redis():
    redis = pytest.importorskip("redis")
    return redis.Redis.from_url(REDIS_URL)


def test_a_short_signing_key_is_refused():
    with pytest.raises(ValueError):
        RedisCache(None, signing_key=b"short")


@needs_redis
def test_redis_cache_shares_expires_and_deletes_by_prefix():
    client, ns = _redis(), f"ntk-{uuid.uuid4().hex}"
    cache = RedisCache(client)
    cache.set_many({f"{ns}:a": b"1", f"{ns}:b": b"2", f"{ns}x:c": b"3"}, ttl=30)
    assert cache.get_many([f"{ns}:a", f"{ns}:b", f"{ns}:missing"]) == [b"1", b"2", None]
    assert 0 < client.pttl(f"{ns}:a") <= 30_000
    cache.delete_prefix(f"{ns}:")
    assert cache.get_many([f"{ns}:a", f"{ns}:b", f"{ns}x:c"]) == [None, None, b"3"]
    cache.delete_prefix(f"{ns}x:")


@needs_redis
def test_redis_cache_ignores_unsigned_tampered_and_foreign_entries():
    client, ns = _redis(), f"ntk-{uuid.uuid4().hex}"
    cache = RedisCache(client, signing_key=b"k" * 32)
    cache.set_many({f"{ns}:a": b"true"}, ttl=30)
    assert cache.get_many([f"{ns}:a"]) == [b"true"]
    client.set(f"{ns}:b", b"true")  # written without the key
    signed = client.get(f"{ns}:a")
    client.set(f"{ns}:c", signed)  # a valid value copied under another key
    client.set(f"{ns}:d", signed[:-4] + b"fals")  # tampered
    assert cache.get_many([f"{ns}:b", f"{ns}:c", f"{ns}:d"]) == [None, None, None]
    assert RedisCache(client, signing_key=b"z" * 32).get_many([f"{ns}:a"]) == [None]
    cache.delete_prefix(f"{ns}:")


@needs_redis
def test_replicas_share_decisions_through_redis():
    namespace, clock = f"ntk-{uuid.uuid4().hex}", Clock()
    clock.now = time.time()  # shared entries carry wall-clock expiry times
    shared = RedisCache(_redis(), signing_key=b"s" * 32)
    one, two = Counting(), Counting()
    a = CachingAuthorizer(one, shared=shared, namespace=namespace, clock=clock)
    b = CachingAuthorizer(two, shared=shared, namespace=namespace, clock=clock)
    assert a.check(BOB, HITS) == [True, False] and b.check(BOB, HITS) == [True, False]
    first = a.filter_for(BOB)  # a asks first; b then finds it in Redis
    assert b.filter_for(BOB) == first and (two.checks, two.filters) == (0, 0)
    a.invalidate(BOB)
    clock.now += 1.1  # b's own copy has expired (local_ttl), and Redis no longer has the entry
    b.check(BOB, HITS)
    assert two.checks == 2
    a.invalidate()


def test_a_shared_filter_nested_too_deep_is_a_miss():
    shared = FakeShared()
    _, (_one, two), (a, b) = _replicas(shared)
    a.filter_for(BOB)
    deep = '{"in": {"field": "team", "values": ["a"]}}'
    for _ in range(150):
        deep = '{"and": [' + deep + "]}"
    entry = '{"expires": 1e12, "epoch": [null, null], "value": {"filter": ' + deep + "}}"
    shared.data = dict.fromkeys(shared.data, entry.encode())
    assert b.filter_for(BOB) is not None and two.filters == 1


def test_stats_count_shared_hits_and_failures():
    shared = FakeShared()
    _, (_one, _two), (a, b) = _replicas(shared)
    a.check(BOB, HITS)
    b.check(BOB, HITS)  # both answers from the shared cache
    assert (b.stats().shared_hits, b.stats().misses) == (2, 0)
    shared.fail = True
    c = CachingAuthorizer(Counting(), shared=shared)
    c.check(BOB, HITS)  # the read fails, so the authorizer is asked, and nothing is written
    assert (c.stats().shared_failures, c.stats().misses) == (1, 2)
