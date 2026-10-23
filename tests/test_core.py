from __future__ import annotations

import asyncio
import copy
import json
import pickle

import pytest

from wardowl import (
    And,
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


@pytest.mark.parametrize("bad", [lambda: Subject(42), lambda: Subject(b"bob"), lambda: Subject("bob", ["eng", 7])])
def test_subject_ids_and_groups_are_strings(bad):
    # 7 and "7" are different filter values, so a group of another type would match differently.
    with pytest.raises(TypeError):
        bad()


def test_subject_attributes_cannot_change_once_the_subject_is_made():
    roles, org = ["reader"], {"region": "eu"}
    attributes = {"tenant": "acme", "roles": roles, "org": org, "tags": {"a"}}
    subject = Subject("bob", ["eng"], attributes)
    attributes["tenant"], org["region"] = "globex", "us"
    roles.append("admin")
    assert subject.attributes == {"tenant": "acme", "roles": ("reader",), "org": {"region": "eu"}, "tags": {"a"}}
    changes = [
        lambda: subject.attributes.__setitem__("tenant", "globex"),
        lambda: subject.attributes.update(tenant="globex"),
        lambda: subject.attributes.pop("tenant"),
        lambda: subject.attributes.clear(),
        lambda: subject.attributes["org"].__setitem__("region", "us"),
        lambda: subject.attributes["org"].__ior__({"region": "us"}),
    ]
    for change in changes:
        with pytest.raises(TypeError):
            change()
    assert subject.attributes["tenant"] == "acme" and subject.attributes["org"] == {"region": "eu"}
    # Still a dict for the authorizers that send attributes to a permission service, and copyable.
    plain = Subject("ann", [], {"tenant": "acme", "roles": ["reader"], "org": {"region": "eu"}})
    assert json.loads(json.dumps(plain.attributes)) == {"tenant": "acme", "roles": ["reader"], "org": {"region": "eu"}}
    assert pickle.loads(pickle.dumps(subject)) == subject == copy.deepcopy(subject)  # noqa: S301


def test_a_decision_cached_for_a_subject_is_not_reused_after_its_attributes_change():
    tenants = ["acme"]
    bob = Subject("bob", [], {"tenants": tenants})
    authz = CachingAuthorizer(FunctionAuthorizer(lambda s, hits, a: ["acme" in s.attributes["tenants"]] * len(hits)))
    hits = [Hit("plan", "t", 1.0, {})]
    assert authz.check(bob, hits) == [True]
    tenants.remove("acme")  # the caller's list, changed after the decision was cached
    assert authz.check(Subject("bob", [], {"tenants": []}), hits) == [False]


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
