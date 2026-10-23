"""CachingAuthorizer's less travelled paths: whatever goes wrong in the cache, it only ever misses."""

from __future__ import annotations

import asyncio

import pytest

from wardowl import (
    AllOf,
    AuthorizationError,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    Or,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)
from wardowl.caching import RedisCache

NS = "test-policy"
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
    assert CachingAuthorizer(one, shared=shared, namespace=NS).filter_for(BOB) is None
    assert CachingAuthorizer(two, shared=shared, namespace=NS).filter_for(BOB) is None
    assert (one.filters, two.filters) == (1, 0)  # the second replica read "no filter" from Redis


@pytest.mark.parametrize("answer", ["everything", Or(In("team", ["a"]), "everything")])
def test_an_answer_that_is_not_a_filter_is_never_cached_and_fails_closed(answer):
    class Bogus(Counting):
        def filter_for(self, subject, action="read"):
            self.filters += 1
            return answer

    shared, bogus = Shared(), Bogus()
    authz = CachingAuthorizer(bogus, shared=shared, namespace=NS)
    retriever = PermissionedRetriever(InMemoryBackend([Document("d", "leave", {})]), authz)
    for _ in range(2):
        with pytest.raises((AuthorizationError, TypeError)):  # refused by the retriever, or by the store
            retriever.retrieve("leave", BOB)
    assert bogus.filters == 2 and shared.data == {}  # asked again: neither cache kept it


class Opaque:
    """A value a cache key cannot encode exactly: repr() would name it by its address."""


@pytest.mark.parametrize("shared", [None, Shared()])
def test_a_chunk_or_subject_holding_a_value_the_key_cannot_encode_is_never_cached(shared):
    inner = Counting()
    authz = CachingAuthorizer(inner, shared=shared, namespace=NS)
    odd = Hit("a", "t", 1.0, {"allowed_groups": ["eng"], "blob": Opaque()})
    good = Hit("b", "t", 1.0, {"allowed_groups": ["hr"]})
    assert authz.check(BOB, [odd]) == [True] and authz.check(BOB, [odd]) == [True]
    assert authz.check(BOB, [good, odd]) == [False, True] and authz.check(BOB, [good, odd]) == [False, True]
    assert asyncio.run(authz.acheck(BOB, [good, odd])) == [False, True]
    assert inner.checks == 1 + 1 + 2 + 1 + 1  # the odd chunk every time; the good one once
    session = Subject("bob", ["eng"], {"session": Opaque()})
    assert authz.filter_for(session) == authz.filter_for(session) and inner.filters == 2
    assert authz.check(session, [good]) == [False] and authz.check(session, [good]) == [False]
    assert inner.checks == 6 + 2


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"allowed_groups": {1}}, {"allowed_groups": "{1}"}),  # repr() of a set, and that text
        ({1: "eng"}, {"1": "eng"}),  # JSON turns the key 1 into "1"
        ({"tags": ["a", "b"]}, {"tags": ("a", "b")}),
        ({"level": 1}, {"level": True}),
        ({"level": 1}, {"level": 1.0}),
    ],
)
def test_chunks_whose_metadata_differs_only_in_type_or_layout_are_decided_separately(first, second):
    calls = []

    def deny(subject, hits, action):
        calls.append(len(hits))
        return [False] * len(hits)

    shared = Shared()
    a, b = (CachingAuthorizer(FunctionAuthorizer(deny), shared=shared, namespace=NS) for _ in range(2))
    a.check(BOB, [Hit("d", "t", 1.0, first)])
    b.check(BOB, [Hit("d", "t", 1.0, second)])  # not the shared answer for the first chunk
    local = CachingAuthorizer(FunctionAuthorizer(deny))
    local.check(BOB, [Hit("d", "t", 1.0, first)])
    local.check(BOB, [Hit("d", "t", 1.0, second)])  # nor this process's
    assert len(calls) == 4


def test_subjects_whose_attributes_differ_only_in_type_are_decided_separately():
    seen = []

    def deny(subject, hits, action):
        seen.append(dict(subject.attributes))
        return [False] * len(hits)

    authz = CachingAuthorizer(FunctionAuthorizer(deny))
    for clearance in (1, True, 1.0, "1"):
        authz.check(Subject("bob", ["eng"], {"clearance": clearance}), HITS[:1])
    assert len(seen) == 4  # Python says 1 == True == 1.0; a policy reading JSON does not


def test_the_parts_of_a_key_cannot_run_into_each_other():
    # Joined with NUL and no lengths, the action "read" with the chunk "d\x00x" and the action
    # "read\x00d" with the chunk "x" had the same key in the shared cache.
    actions = []

    def deny(subject, hits, action):
        actions.append(action)
        return [False] * len(hits)

    shared = Shared()
    a, b = (CachingAuthorizer(FunctionAuthorizer(deny), shared=shared, namespace=NS) for _ in range(2))
    a.check(BOB, [Hit("d\x00x", "t", 1.0, {})], "read")
    b.check(BOB, [Hit("x", "t", 1.0, {})], "read\x00d")
    assert actions == ["read", "read\x00d"]


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
        assert CachingAuthorizer(inner, shared=shared, namespace=NS).check(BOB, HITS) == [True, False]
        assert inner.checks == 2  # asked the authorizer instead


def test_a_failing_shared_write_never_changes_an_answer():
    class ReadOnly(Shared):
        def set_many(self, entries, ttl):
            raise ConnectionError("read-only replica")

    inner = Counting()
    authz = CachingAuthorizer(inner, shared=ReadOnly(), namespace=NS)
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
