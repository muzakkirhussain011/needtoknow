from __future__ import annotations

import asyncio

import pytest

from needtoknow import (
    And,
    AuthorizationError,
    Document,
    FunctionAuthorizer,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    Or,
    PermissionedRetriever,
    Subject,
    matches,
)

ALICE = Subject("alice", groups=["hr"])
BOB = Subject("bob", groups=["eng"])


def corpus() -> list[Document]:
    return [
        Document("handbook", "leave policy for all staff", {"allowed_groups": ["*"]}),
        Document("salaries", "salary bands and leave payouts", {"allowed_groups": ["hr"]}),
        Document("runbook", "incident response and deploy rollback", {"allowed_groups": ["eng"]}),
        Document("bob-review", "performance review leave notes", {"allowed_users": ["bob"]}),
        Document("orphan", "leave policy draft with no permissions"),
    ]


# ---- filters and types -------------------------------------------------------------------------


def test_in_matches_scalars_and_lists():
    assert matches(In("team", {"eng"}), {"team": "eng"})
    assert matches(In("groups", {"eng"}), {"groups": ["hr", "eng"]})
    assert not matches(In("groups", {"eng"}), {"groups": []})
    assert not matches(In("team", {"eng"}), {})


def test_empty_and_or_are_rejected_because_empty_and_means_allow_all():
    with pytest.raises(ValueError):
        And()
    with pytest.raises(ValueError):
        Or()


def test_nested_filters():
    f = And(In("tenant", {"acme"}), Or(In("team", {"eng"}), In("level", {"public"})))
    assert matches(f, {"tenant": "acme", "level": "public"})
    assert not matches(f, {"tenant": "other", "team": "eng"})


def test_subject_rejects_a_single_string_for_groups():
    with pytest.raises(TypeError):
        Subject("bob", groups="eng")
    with pytest.raises(ValueError):
        Subject("")


def test_subjects_are_hashable_for_caching():
    assert hash(Subject("bob", ["eng"])) == hash(Subject("bob", ["eng"], {"region": "eu"}))


# ---- metadata ACL ------------------------------------------------------------------------------


def test_metadata_acl_user_group_everyone_and_missing_permissions():
    backend = InMemoryBackend(corpus())
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    ids = {h.id for h in retriever.retrieve("leave", BOB, k=10).hits}
    assert ids == {"handbook", "bob-review"}  # not hr-only, not the chunk without permissions
    ids = {h.id for h in retriever.retrieve("leave", ALICE, k=10).hits}
    assert ids == {"handbook", "salaries"}


def test_unsupported_action_fails_closed():
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), MetadataACLAuthorizer(), action="delete")
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)


# ---- strategies --------------------------------------------------------------------------------


def test_auto_uses_prefilter_when_possible_and_postfilter_otherwise():
    backend = InMemoryBackend(corpus())
    assert PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve("leave", BOB).strategy == "prefilter"
    only_check = FunctionAuthorizer(lambda s, hits, a: [h.metadata.get("allowed_groups") == ["*"] for h in hits])
    assert PermissionedRetriever(backend, only_check).retrieve("leave", BOB).strategy == "postfilter"


def test_prefilter_requires_a_filter_capable_authorizer():
    only_check = FunctionAuthorizer(lambda s, hits, a: [True] * len(hits))
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), only_check, strategy="prefilter")
    with pytest.raises(ValueError):
        retriever.retrieve("leave", BOB)


def test_prefilter_requires_a_filter_capable_backend():
    class NoFilters(InMemoryBackend):
        supports_filters = False

    with pytest.raises(ValueError):
        PermissionedRetriever(NoFilters(corpus()), MetadataACLAuthorizer(), strategy="prefilter")


def test_postfilter_widens_until_it_finds_k_permitted_hits():
    # 100 matching chunks; Bob may read only the 3 lowest-ranked ones.
    docs = [Document(f"d{i:03d}", "leave " * (1 + (i % 5)), {"allowed_groups": ["hr"]}) for i in range(97)]
    docs += [Document(f"z{i}", "leave", {"allowed_users": ["bob"]}) for i in range(3)]
    retriever = PermissionedRetriever(InMemoryBackend(docs), MetadataACLAuthorizer(), strategy="postfilter")
    result = retriever.retrieve("leave", BOB, k=3)
    assert sorted(h.id for h in result.hits) == ["z0", "z1", "z2"]
    assert result.complete and result.denied >= 97


def test_postfilter_reports_incomplete_results_when_capped():
    docs = [Document(f"d{i:03d}", "leave", {"allowed_groups": ["hr"]}) for i in range(50)]
    docs.append(Document("zzz", "leave", {"allowed_users": ["bob"]}))
    retriever = PermissionedRetriever(
        InMemoryBackend(docs), MetadataACLAuthorizer(), strategy="postfilter", max_candidates=20
    )
    result = retriever.retrieve("leave", BOB, k=1)
    assert result.hits == [] and result.complete is False


# ---- failing closed ----------------------------------------------------------------------------


def _raises(*_args):
    raise ConnectionError("permission service unreachable")


@pytest.mark.parametrize("strategy", ["prefilter", "postfilter"])
def test_authorizer_errors_return_nothing(strategy):
    authorizer = FunctionAuthorizer(_raises, filter_for=_raises if strategy == "prefilter" else None)
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy=strategy)
    with pytest.raises(AuthorizationError, match="unreachable"):
        retriever.retrieve("leave", BOB)


@pytest.mark.parametrize(
    "decisions",
    [
        lambda hits: [True] * (len(hits) + 1),  # wrong length
        lambda hits: ["yes"] * len(hits),  # truthy non-bools must not count as allow
        lambda hits: [1] * len(hits),
    ],
)
def test_malformed_decisions_fail_closed(decisions):
    authorizer = FunctionAuthorizer(lambda s, hits, a: decisions(hits))
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy="postfilter")
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)


def test_invalid_arguments():
    backend, authz = InMemoryBackend(corpus()), MetadataACLAuthorizer()
    with pytest.raises(ValueError):
        PermissionedRetriever(backend, authz, strategy="fastest")
    with pytest.raises(ValueError):
        PermissionedRetriever(backend, authz, overfetch=1)
    with pytest.raises(ValueError):
        PermissionedRetriever(backend, authz).retrieve("leave", BOB, k=0)


# ---- async -------------------------------------------------------------------------------------


def test_native_async_backend_and_authorizer_are_used():
    calls = []

    class AsyncBackend(InMemoryBackend):
        async def asearch(self, query, k, filter=None):
            calls.append("asearch")
            return self.search(query, k, filter)

    class AsyncAuthorizer(MetadataACLAuthorizer):
        async def afilter_for(self, subject, action="read"):
            calls.append("afilter_for")
            return self.filter_for(subject, action)

        async def acheck(self, subject, hits, action="read"):
            calls.append("acheck")
            return self.check(subject, hits, action)

    retriever = PermissionedRetriever(AsyncBackend(corpus()), AsyncAuthorizer())
    result = asyncio.run(retriever.aretrieve("leave", BOB, k=10))
    assert {h.id for h in result.hits} == {"handbook", "bob-review"}
    assert calls == ["afilter_for", "asearch", "acheck"]


def test_async_authorizer_errors_fail_closed():
    authorizer = FunctionAuthorizer(_raises)
    retriever = PermissionedRetriever(InMemoryBackend(corpus()), authorizer, strategy="postfilter")
    with pytest.raises(AuthorizationError):
        asyncio.run(retriever.aretrieve("leave", BOB))
