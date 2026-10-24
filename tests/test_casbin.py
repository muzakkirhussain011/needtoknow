"""CasbinAuthorizer: the metadata ACL model as Casbin policies, compared with the reference answer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import AuthorizationError, Hit, InMemoryBackend, PermissionedRetriever, Subject
from wardowl.authorizers.casbin import CasbinAuthorizer
from wardowl.testing import check_authorizer

casbin = pytest.importorskip("casbin")
from casbin.model import Model  # noqa: E402

# RBAC, plus "*" for everyone: the reference model of tests/helpers.py.
MODEL = """
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = (g(r.sub, p.sub) || p.sub == "*") && r.obj == p.obj && r.act == p.act
"""


def enforcer(policies=(), groups=()):
    model = Model()
    model.load_model_from_text(MODEL)
    e = casbin.Enforcer(model)
    for p in policies:
        e.add_policy(*p)
    for g in groups:
        e.add_grouping_policy(*g)
    return e


def hit(id_, doc=None):
    return Hit(id_, "text", 1.0, {"doc_id": doc} if doc else {})


@settings(max_examples=60, deadline=None)
@given(docs=corpora(max_size=15), subject=subjects, query=queries, k=st.integers(min_value=1, max_value=6))
def test_policies_written_from_the_acls_give_exactly_the_reference_answer(docs, subject, query, k):
    policies = [(u, d.id, "read") for d in docs for u in d.metadata.get("allowed_users", [])]
    # Groups carry the group prefix; "*" is the model's everyone.
    policies += [
        (g if g == "*" else f"group:{g}", d.id, "read") for d in docs for g in d.metadata.get("allowed_groups", [])
    ]
    authz = CasbinAuthorizer(enforcer(policies), resource_field=None, group_subjects=True)
    retriever = PermissionedRetriever(InMemoryBackend(docs), authz, max_candidates=len(docs) + k)
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


def test_memberships_kept_in_casbin_and_a_chunk_without_its_object():
    e = enforcer([("hr", "pay", "read"), ("ann", "plan", "read")], groups=[("hana", "hr")])
    authz = CasbinAuthorizer(e)
    hits = [hit("c1", "plan"), hit("c2", "pay"), hit("c3")]
    assert authz.check(Subject("ann"), hits) == [True, False, False]
    assert authz.check(Subject("hana"), hits) == [False, True, False]
    assert authz.check(Subject("zed"), [hit("c3")]) == [False]


def test_groups_are_prefixed_so_they_cannot_pass_for_a_user_or_a_role():
    # "admin" is a role and "ceo@corp.com" a user. A token group of the same name used to be sent as
    # the bare subject, and got that role's or that user's access.
    policies = [("admin", "secrets", "read"), ("ceo@corp.com", "board", "read"), ("group:eng", "spec", "read")]
    authz = CasbinAuthorizer(enforcer(policies, groups=[("ann", "admin")]), group_subjects=True)
    hits = [hit("c1", "secrets"), hit("c2", "board"), hit("c3", "spec")]
    assert authz.check(Subject("mallory", ["admin", "ceo@corp.com", "eng"]), hits) == [False, False, True]
    assert authz.check(Subject("ann"), hits) == [True, False, False]
    assert authz.check(Subject("ceo@corp.com"), hits) == [False, True, False]
    with pytest.raises(AuthorizationError, match="group prefix"):
        authz.check(Subject("group:eng"), hits)  # would pass for the group
    teams = CasbinAuthorizer(enforcer([("team/eng", "spec", "read")]), group_subjects=True, group_prefix="team/")
    assert teams.check(Subject("bob", ["eng"]), hits) == [False, False, True]
    with pytest.raises(ValueError, match="group_prefix"):
        CasbinAuthorizer(enforcer(), group_subjects=True, group_prefix="")


def test_a_custom_request_for_abac_models():
    requests = []

    class Recording:
        def enforce(self, *r):
            requests.append(r)
            return r[1].metadata.get("level", 9) <= r[0].clearance

    def request(subject, hit, action):
        return (SimpleNamespace(clearance=subject.attributes["clearance"]), hit, action)

    authz = CasbinAuthorizer(Recording(), request=request)
    levels = [Hit("a", "", 1.0, {"level": 1}), Hit("b", "", 1.0, {"level": 3})]
    assert authz.check(Subject("ann", attributes={"clearance": 2}), levels) == [True, False]
    assert len(requests) == 2


@pytest.mark.parametrize(
    "answer",
    [RuntimeError("model error"), "yes", [True], [True, False, "x"]],
)
def test_a_failing_or_malformed_enforcer_raises(answer):
    class Broken:
        def enforce(self, *r):
            return True

        def batch_enforce(self, requests):
            if isinstance(answer, Exception):
                raise answer
            return answer if isinstance(answer, list) else [answer] * len(requests)

    with pytest.raises(AuthorizationError) as failure:
        CasbinAuthorizer(Broken()).check(Subject("ann"), [hit("c1", "plan"), hit("c2", "pay")])
    assert "model error" not in str(failure.value)  # an enforcer's text stays in the cause


def test_actions_configuration_and_no_pre_filter():
    authz = CasbinAuthorizer(enforcer([("ann", "plan", "view")]), actions={"read": "view"})
    assert authz.check(Subject("ann"), [hit("c1", "plan")]) == [True]
    assert authz.filter_for(Subject("ann")) is None
    with pytest.raises(AuthorizationError, match="write"):
        authz.check(Subject("ann"), [hit("c1", "plan")], action="write")
    with pytest.raises(AuthorizationError, match="write"):
        authz.filter_for(Subject("ann"), action="write")
    with pytest.raises(TypeError):
        CasbinAuthorizer(object())


def test_the_conformance_kit():
    check_authorizer(CasbinAuthorizer(enforcer([("ann", "plan", "read")])))
