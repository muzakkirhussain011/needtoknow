"""CachingAuthorizer's less travelled paths: whatever goes wrong in the cache, it only ever misses."""

from __future__ import annotations

import asyncio

import pytest

from wardowl import (
    AllOf,
    AuthorizationError,
    CachingAuthorizer,
    Document,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)
from wardowl.caching import RedisCache

BOB = Subject("bob", ["eng"])
HITS = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "t", 1.0, {"allowed_groups": ["hr"]})]


class Shared:
    """A dictionary standing in for Redis."""

    def __init__(self):
        self.data: dict[str, bytes] = {}

    def get_many(self, keys):
        return [self.data.get(k) for k in keys]

    def set_many(self, entries, ttl):
        self.data.update(entries)

    def delete_prefix(self, prefix):
        for key in [k for k in self.data if k.startswith(prefix)]:
            del self.data[key]


class Counting:
    def __init__(self, inner=None):
        self.inner = inner or MetadataACLAuthorizer()
        self.checks = self.filters = 0

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        return self.inner.check(subject, hits, action)

    def filter_for(self, subject, action="read"):
        self.filters += 1
        return self.inner.filter_for(subject, action)


class NoFilter(Counting):
    """An authorizer that cannot express its rules as a filter."""

    def filter_for(self, subject, action="read"):
        self.filters += 1
        return None


def test_no_filter_is_an_answer_too_and_is_shared():
    shared = Shared()
    one, two = NoFilter(), NoFilter()
    assert CachingAuthorizer(one, shared=shared).filter_for(BOB) is None
    assert CachingAuthorizer(two, shared=shared).filter_for(BOB) is None
    assert (one.filters, two.filters) == (1, 0)  # the second replica read "no filter" from Redis


def test_an_answer_that_is_not_a_filter_is_kept_out_of_the_shared_cache_and_fails_closed():
    class Bogus(Counting):
        def filter_for(self, subject, action="read"):
            return "everything"

    shared = Shared()
    authz = CachingAuthorizer(Bogus(), shared=shared)
    retriever = PermissionedRetriever(InMemoryBackend([Document("d", "leave", {})]), authz)
    for _ in range(2):  # the second time from this process's cache, with the same result
        with pytest.raises(AuthorizationError):
            retriever.retrieve("leave", BOB)
    assert shared.data == {}


def test_a_shared_cache_returning_text_or_the_wrong_count_is_a_miss():
    class Text(Shared):
        def get_many(self, keys):
            return ["true" for _ in keys]

    class Short(Shared):
        def get_many(self, keys):  # one value short, except for the global epoch read alone
            values = super().get_many(keys)
            return values[:-1] if len(keys) > 1 else values

    for shared in (Text(), Short()):
        inner = Counting()
        assert CachingAuthorizer(inner, shared=shared).check(BOB, HITS) == [True, False]
        assert inner.checks == 2  # asked the authorizer instead


def test_a_failing_shared_write_never_changes_an_answer():
    class ReadOnly(Shared):
        def set_many(self, entries, ttl):
            raise ConnectionError("read-only replica")

    inner = Counting()
    authz = CachingAuthorizer(inner, shared=ReadOnly())
    assert authz.check(BOB, HITS) == [True, False]
    assert authz.check(BOB, HITS) == [True, False]
    assert inner.checks == 2  # this process kept its own copy


def test_metadata_that_json_cannot_encode_still_gets_a_cache_key():
    inner = Counting()
    authz = CachingAuthorizer(inner)
    odd = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"], ("tuple", "key"): 1})]
    assert authz.check(BOB, odd) == [True] and authz.check(BOB, odd) == [True]
    assert inner.checks == 1


def test_async_filters_and_scopes_use_native_methods_threads_or_nothing():
    class Native(Counting):
        async def afilter_for(self, subject, action="read"):
            self.filters += 1
            return In("allowed_groups", ["eng"])

        async def ascope_for(self, subject, action="read"):
            return In("tenant", ["acme"])

    native = Native()
    cache = CachingAuthorizer(native)
    assert asyncio.run(cache.afilter_for(BOB)) == In("allowed_groups", ["eng"])
    assert asyncio.run(cache.afilter_for(BOB)) == In("allowed_groups", ["eng"]) and native.filters == 1
    assert asyncio.run(cache.ascope_for(BOB)) == In("tenant", ["acme"])
    tenant = Subject("bob", ["eng"], {"tenant": "acme"})
    in_a_thread = CachingAuthorizer(AllOf(TenantAuthorizer(), MetadataACLAuthorizer()))
    assert asyncio.run(in_a_thread.ascope_for(tenant)) == in_a_thread.scope_for(tenant)
    assert asyncio.run(CachingAuthorizer(MetadataACLAuthorizer()).ascope_for(BOB)) is None


def test_redis_deletes_a_large_prefix_in_batches():
    class Client:
        def __init__(self):
            self.unlinked, self.patterns = [], []

        def scan_iter(self, match, count):
            self.patterns.append(match)
            return (f"ns:{i}" for i in range(1200))

        def unlink(self, *keys):
            self.unlinked.append(len(keys))

    client = Client()
    RedisCache(client).delete_prefix("ns:[1]")
    assert client.unlinked == [500, 500, 200]
    assert client.patterns == ["ns:\\[1\\]*"]  # glob characters in the prefix are matched literally
