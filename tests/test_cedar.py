"""CedarAuthorizer: the metadata ACL model written in Cedar, compared with the reference answer, and
every way a decision can fail."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import AuthorizationError, Hit, InMemoryBackend, PermissionedRetriever, Subject
from wardowl.authorizers.cedar import _SKIP, CedarAuthorizer, _value
from wardowl.testing import check_authorizer

cedarpy = pytest.importorskip("cedarpy")

# The reference model of tests/helpers.py: a listed user, a listed group, or "*".
ACL = """
permit (principal, action == Action::"read", resource)
when { resource has allowed_users && resource.allowed_users.contains(principal.id) };

permit (principal, action == Action::"read", resource)
when {
  resource has allowed_groups &&
  (resource.allowed_groups.contains("*") || resource.allowed_groups.containsAny(principal.groups))
};
"""
ANN = Subject("ann", ["eng"], {"region": "eu"})


def hit(id_, **metadata):
    return Hit(id_, "", 1.0, metadata)


def entity(entity_type, entity_id, parents=()):
    return {
        "uid": {"__entity": {"type": entity_type, "id": entity_id}},
        "attrs": {},
        "parents": [{"__entity": {"type": t, "id": i}} for t, i in parents],
    }


@settings(max_examples=60, deadline=None)
@given(
    docs=corpora(max_size=15),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "postfilter"]),
)
def test_the_acl_policy_gives_exactly_the_reference_answer(docs, subject, query, k, strategy):
    authz = CedarAuthorizer(ACL)
    retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + 1)
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


def test_the_acl_policy_passes_the_conformance_kit():
    check_authorizer(CedarAuthorizer(ACL))


def test_there_is_no_pre_filter_so_the_retriever_post_filters():
    authz = CedarAuthorizer(ACL)
    assert authz.filter_for(ANN) is None
    assert authz.check(ANN, []) == []


def test_groups_inherit_from_parent_groups_given_as_entities():
    policy = 'permit (principal in Group::"finance", action == Action::"read", resource);'
    payroll = entity("Group", "payroll", [("Group", "finance")])
    authz = CedarAuthorizer(policy, entities=lambda subject: [payroll])
    assert authz.check(Subject("ann", ["payroll"]), [hit("a")]) == [True]
    assert authz.check(Subject("bob", ["sales"]), [hit("a")]) == [False]


def test_subject_attributes_and_metadata_reach_the_policy_as_cedar_values():
    policy = """permit (principal, action, resource) when {
      principal.region == "eu" && resource.level <= 3 && resource.public &&
      resource.owner.team == "eng" && resource.tags.contains("x")
    };"""
    owner = {"team": "eng", "__entity": {"type": "User", "id": "root"}, 7: "ignored"}
    good = hit("a", level=2, public=True, owner=owner, tags=["x", "y"])
    authz = CedarAuthorizer(policy)
    assert authz.check(ANN, [good, hit("b", level=5, public=True, owner=owner, tags=["x"])]) == [True, False]
    assert authz.check(Subject("bob", [], {"region": "us"}), [good]) == [False]


def test_values_cedar_cannot_hold_are_left_out():
    assert _value(2**70) is _SKIP and _value(-(2**70)) is _SKIP
    assert _value(1.5) is _SKIP and _value(None) is _SKIP and _value([1, 1.5]) is _SKIP
    assert _value((1, "a", True)) == [1, "a", True]
    assert _value({"a": None, "b": {"c": 2}}) == {"b": {"c": 2}}


def test_a_chunk_whose_evaluation_errors_is_denied_even_when_a_permit_applies(caplog):
    # Cedar skips a forbid that errors. Here it errors on chunks without a classification, or with
    # one Cedar cannot hold, and the permit alone would allow them.
    policy = """
    permit (principal, action, resource);
    forbid (principal, action, resource) when { resource.classification == "secret" };
    """
    hits = [
        hit("a", classification="public"),
        hit("b"),
        hit("c", classification=0.5),
        hit("d", classification="secret"),
    ]
    with caplog.at_level(logging.WARNING, logger="wardowl"):
        assert CedarAuthorizer(policy).check(ANN, hits) == [True, False, False, False]
    assert "2 of 4 chunks" in caplog.text


def test_decisions_keep_their_order_across_batches():
    hits = [hit(f"d{i}", allowed_groups=["eng"] if i % 2 else ["hr"]) for i in range(7)]
    assert CedarAuthorizer(ACL, batch_size=2).check(ANN, hits) == [i % 2 == 1 for i in range(7)]


def test_the_same_chunk_twice_is_decided_once_but_conflicting_copies_are_refused():
    authz = CedarAuthorizer(ACL)
    assert authz.check(ANN, [hit("a", allowed_groups=["eng"])] * 2) == [True, True]
    with pytest.raises(AuthorizationError, match="different metadata"):
        authz.check(ANN, [hit("a", allowed_groups=["eng"]), hit("a", allowed_groups=["hr"])])


@pytest.mark.parametrize("name", ["id", "groups"])
def test_a_subject_attribute_that_would_replace_the_subject_is_refused(name):
    with pytest.raises(AuthorizationError, match=name):
        CedarAuthorizer(ACL).check(Subject("ann", ["eng"], {name: "x"}), [hit("a")])


def test_an_action_without_a_cedar_action_is_refused():
    authz = CedarAuthorizer(ACL)
    with pytest.raises(AuthorizationError, match="write"):
        authz.check(ANN, [hit("a")], action="write")
    with pytest.raises(AuthorizationError, match="write"):
        authz.filter_for(ANN, action="write")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"policies": "permit ("},
        {"policies": "  "},
        {"policies": ACL, "resource_type": "Bad Type"},
        {"policies": ACL, "actions": {"read": 'a"b'}},
        {"policies": ACL, "batch_size": 0},
    ],
)
def test_bad_configuration_is_refused_when_created(kwargs):
    with pytest.raises(ValueError):
        CedarAuthorizer(**kwargs)


def test_failing_or_malformed_entities_raise():
    def broken(subject):
        raise RuntimeError("directory down")

    with pytest.raises(AuthorizationError, match="entities failed"):
        CedarAuthorizer(ACL, entities=broken).check(ANN, [hit("a")])
    with pytest.raises(AuthorizationError, match="Cedar"):
        CedarAuthorizer(ACL, entities=[{"uid": "nonsense"}]).check(ANN, [hit("a")])


def result(correlation_id="0", decision=None, errors=()):
    return SimpleNamespace(
        correlation_id=correlation_id,
        decision=decision or cedarpy.Decision.Allow,
        diagnostics=SimpleNamespace(errors=list(errors)),
    )


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (RuntimeError("engine crashed"), "Cedar failed"),
        ([], "unexpected number"),
        ([result(correlation_id="9")], "different request"),
        ([result(decision=cedarpy.Decision.NoDecision)], "no decision"),
    ],
)
def test_a_failing_or_inconsistent_engine_raises(answer, message):
    def batch(requests, policies, entities, schema=None):
        if isinstance(answer, Exception):
            raise answer
        return answer

    authz = CedarAuthorizer(ACL)
    authz._cedar = SimpleNamespace(Decision=cedarpy.Decision, is_authorized_batch=batch)
    with pytest.raises(AuthorizationError, match=message):
        authz.check(ANN, [hit("a")])
