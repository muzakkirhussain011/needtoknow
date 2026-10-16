"""AllOf, AnyOf, TenantAuthorizer, and post-filtering scoped to a necessary condition."""

from __future__ import annotations

import asyncio

import pytest
from helpers import corpora, may_read, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import (
    AllOf,
    And,
    AnyOf,
    AuthorizationError,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)
from needtoknow.testing import check_authorizer

TENANTS = ["acme", "globex"]


def _with_tenants(docs, draw_tenants):
    return [Document(d.id, d.text, {**d.metadata, "tenant": t}) for d, t in zip(docs, draw_tenants, strict=True)]


def _oracle(docs, query, k, keep):
    readable = InMemoryBackend([d for d in docs if keep(d)])
    return [h.id for h in readable.search(query, k)]


@st.composite
def tenanted(draw):
    docs = draw(corpora(max_size=15))
    return _with_tenants(docs, draw(st.lists(st.sampled_from(TENANTS), min_size=len(docs), max_size=len(docs))))


def _in_tenant(subject):
    return Subject(subject.id, subject.groups, {"tenant": "acme"})


@settings(max_examples=120, deadline=None)
@given(tenanted(), subjects, queries, st.integers(1, 5), st.sampled_from(["auto", "prefilter", "postfilter"]))
def test_all_of_is_the_intersection(docs, subject, query, k, strategy):
    subject = _in_tenant(subject)
    authz = AllOf(TenantAuthorizer(), MetadataACLAuthorizer())
    retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + 1)
    expected = _oracle(docs, query, k, lambda d: d.metadata["tenant"] == "acme" and may_read(subject, d))
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == expected


@settings(max_examples=120, deadline=None)
@given(tenanted(), subjects, queries, st.integers(1, 5), st.sampled_from(["auto", "prefilter", "postfilter"]))
def test_any_of_is_the_union(docs, subject, query, k, strategy):
    subject = _in_tenant(subject)
    authz = AnyOf(MetadataACLAuthorizer(), TenantAuthorizer())
    retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + 1)
    expected = _oracle(docs, query, k, lambda d: d.metadata["tenant"] == "acme" or may_read(subject, d))
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == expected


class Asked:
    """Records which hits each check was asked about."""

    def __init__(self, decide):
        self.decide, self.asked = decide, []

    def check(self, subject, hits, action="read"):
        self.asked.append([h.id for h in hits])
        return [self.decide(h) for h in hits]

    def filter_for(self, subject, action="read"):
        return None


HITS = [Hit(f"h{i}", "t", 1.0, {}) for i in range(4)]
BOB = Subject("bob", [], {"tenant": "acme"})


def test_later_authorizers_see_only_the_undecided_hits():
    first, second = Asked(lambda h: h.id in ("h0", "h1")), Asked(lambda h: h.id == "h1")
    assert AllOf(first, second).check(BOB, HITS) == [False, True, False, False]
    assert second.asked == [["h0", "h1"]]
    first, second = Asked(lambda h: h.id in ("h0", "h1")), Asked(lambda h: h.id == "h3")
    assert AnyOf(first, second).check(BOB, HITS) == [True, True, False, True]
    assert second.asked == [["h2", "h3"]]
    nobody = Asked(lambda h: False)
    assert AllOf(nobody, Asked(lambda h: True)).check(BOB, HITS) == [False] * 4


def test_filters_combine_only_when_every_part_has_one():
    tenant, acl = TenantAuthorizer(), MetadataACLAuthorizer()
    both = AllOf(tenant, acl).filter_for(BOB)
    assert both == And(In("tenant", ["acme"]), acl.filter_for(BOB))
    no_filter = Asked(lambda h: True)
    assert AllOf(tenant, no_filter).filter_for(BOB) is None
    assert AllOf(tenant, no_filter).scope_for(BOB) == In("tenant", ["acme"])
    assert AllOf(no_filter).scope_for(BOB) is None
    assert AnyOf(tenant, no_filter).scope_for(BOB) is None  # a union needs every part bounded
    assert AnyOf(tenant, acl).scope_for(BOB) == AnyOf(tenant, acl).filter_for(BOB)


class Spy(InMemoryBackend):
    def __init__(self, docs):
        super().__init__(docs)
        self.filters = []

    def search(self, query, k, filter=None):
        self.filters.append(filter)
        return super().search(query, k, filter)


def test_post_filtering_searches_inside_the_scope():
    # 1,000 documents; the subject's tenant holds 10. The fine-grained rule has no filter.
    tenant = {True: "acme", False: "globex"}
    docs = [Document(f"d{i:04d}", "leave policy", {"tenant": tenant[i % 100 == 0]}) for i in range(1000)]
    rule = FunctionAuthorizer(lambda subject, hits, action: [True] * len(hits))
    scoped = Spy(docs)
    result = PermissionedRetriever(scoped, AllOf(TenantAuthorizer(), rule), max_candidates=40).retrieve("leave", BOB, 5)
    assert result.strategy == "postfilter" and result.complete and len(result.hits) == 5
    assert all(h.metadata["tenant"] == "acme" for h in result.hits)
    assert scoped.filters and all(f == In("tenant", ["acme"]) for f in scoped.filters)
    # Without the scope, the same budget of 40 candidates finds almost nothing from the tenant.
    combined = FunctionAuthorizer(lambda s, hits, a: [h.metadata["tenant"] == "acme" for h in hits])
    unscoped = PermissionedRetriever(InMemoryBackend(docs), combined, max_candidates=40).retrieve("leave", BOB, 5)
    assert not unscoped.complete and len(unscoped.hits) < 5


def test_a_subject_without_a_tenant_gets_nothing():
    retriever = PermissionedRetriever(InMemoryBackend(), AllOf(TenantAuthorizer(), MetadataACLAuthorizer()))
    for attributes in ({}, {"tenant": ""}, {"tenant": True}, {"tenant": None}):
        with pytest.raises(AuthorizationError, match="tenant"):
            retriever.retrieve("leave", Subject("bob", [], attributes))
    assert TenantAuthorizer("org", "org_id").filter_for(Subject("bob", [], {"org_id": 7})) == In("org", [7])


def test_malformed_answers_from_a_part_fail_closed():
    short = FunctionAuthorizer(lambda subject, hits, action: [True])
    with pytest.raises(AuthorizationError):
        AllOf(MetadataACLAuthorizer(), short).check(BOB, [Hit("a", "t", 1.0, {"allowed_groups": ["*"]})] * 2)
    with pytest.raises(AuthorizationError):
        AnyOf(FunctionAuthorizer(lambda subject, hits, action: ["yes"] * len(hits))).check(BOB, HITS)
    bogus = FunctionAuthorizer(lambda s, h, a: [True] * len(h), filter_for=lambda s, a: "everything")
    with pytest.raises(AuthorizationError, match="not a filter"):
        PermissionedRetriever(InMemoryBackend([Document("a", "leave")]), bogus).retrieve("leave", BOB)
    with pytest.raises(ValueError):
        AllOf()
    with pytest.raises(ValueError):
        AnyOf()


def test_async_gives_the_same_answers():
    docs = [Document(f"d{i}", "leave", {"tenant": TENANTS[i % 2], "allowed_groups": ["*"]}) for i in range(8)]
    for authz in (
        AllOf(TenantAuthorizer(), CachingAuthorizer(MetadataACLAuthorizer())),
        AnyOf(CachingAuthorizer(TenantAuthorizer()), MetadataACLAuthorizer()),
        AllOf(TenantAuthorizer(), Asked(lambda h: True)),
    ):
        retriever = PermissionedRetriever(InMemoryBackend(docs), authz)
        assert asyncio.run(retriever.aretrieve("leave", BOB, 3)) == retriever.retrieve("leave", BOB, 3)


def test_caching_passes_the_scope_through():
    calls = []

    class Scoped(Asked):
        def scope_for(self, subject, action="read"):
            calls.append(subject.id)
            return In("tenant", ["acme"])

    cached = CachingAuthorizer(Scoped(lambda h: True))
    assert cached.scope_for(BOB) == cached.scope_for(BOB) == In("tenant", ["acme"]) and calls == ["bob"]
    assert asyncio.run(cached.ascope_for(BOB)) == In("tenant", ["acme"]) and calls == ["bob"]
    assert CachingAuthorizer(MetadataACLAuthorizer()).scope_for(BOB) is None


def test_composed_authorizers_pass_the_conformance_kit():
    people = [Subject(u, g, {"tenant": t}) for u, g, t in [("ann", ["hr"], "acme"), ("bob", [], "globex")]]
    hits = [
        Hit(f"c{i}", "t", 1.0, {"tenant": t, "allowed_groups": g})
        for i, (t, g) in enumerate([("acme", ["hr"]), ("acme", ["*"]), ("globex", ["hr"]), ("globex", [])])
    ]
    check_authorizer(AllOf(TenantAuthorizer(), MetadataACLAuthorizer()), people, hits)
    check_authorizer(AnyOf(TenantAuthorizer(), MetadataACLAuthorizer()), people, hits)
