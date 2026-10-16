"""CerbosAuthorizer: plan translation, a scripted PDP, and a real Cerbos when one is available."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import And, AuthorizationError, Hit, In, InMemoryBackend, Or, PermissionedRetriever, Subject, matches
from needtoknow.authorizers.cerbos import CerbosAuthorizer, plan_to_filter
from needtoknow.testing import check_authorizer

ANN = Subject("ann", ["eng"], {"region": "eu"})


def var(field):
    return {"variable": f"request.resource.attr.{field}"}


def val(value):
    return {"value": value}


def expr(operator, *operands):
    return {"expression": {"operator": operator, "operands": list(operands)}}


def conditional(condition):
    return {"kind": "KIND_CONDITIONAL", "condition": condition}


# ---- plan translation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (expr("eq", var("team"), val("eng")), In("team", ["eng"])),
        (expr("eq", val(2), var("level")), In("level", [2])),
        (expr("in", val("ann"), var("allowed_users")), In("allowed_users", ["ann"])),
        (expr("in", var("team"), val(["eng", "hr"])), In("team", ["eng", "hr"])),
        (expr("hasIntersection", var("allowed_groups"), val(["eng", "*"])), In("allowed_groups", ["eng", "*"])),
        (expr("hasIntersection", val(["eng"]), var("allowed_groups")), In("allowed_groups", ["eng"])),
        (expr("hasIntersection", var("allowed_groups"), val([])), In("allowed_groups", [])),
        (
            expr("or", expr("in", val("ann"), var("allowed_users")), expr("eq", var("team"), val("eng"))),
            Or(In("allowed_users", ["ann"]), In("team", ["eng"])),
        ),
        (
            expr("and", expr("eq", var("a"), val("x")), expr("or", expr("eq", var("b"), val(1)))),
            And(In("a", ["x"]), Or(In("b", [1]))),
        ),
    ],
)
def test_supported_plans_become_exact_filters(condition, expected):
    assert plan_to_filter(conditional(condition)) == expected


@pytest.mark.parametrize(
    "condition",
    [
        expr("ne", var("team"), val("eng")),
        expr("not", expr("eq", var("team"), val("eng"))),
        expr("lt", var("level"), val(3)),
        expr("eq", var("public"), val(True)),  # booleans never match a needtoknow filter
        expr("eq", var("score"), val(0.5)),
        expr("eq", {"variable": "request.resource.attr.owner.id"}, val("ann")),  # a nested path
        expr("eq", {"variable": "request.principal.attr.region"}, val("eu")),
        expr("in", var("team"), val("eng")),  # in needs a list on the right
        expr("and", expr("eq", var("a"), val("x")), expr("ne", var("b"), val("y"))),  # one bad clause spoils it
        expr("exists", var("tags"), {"expression": {"operator": "lambda", "operands": []}}),
        {"not": "an expression"},
    ],
)
def test_anything_else_gives_no_filter_so_the_retriever_post_filters(condition):
    assert plan_to_filter(conditional(condition)) is None


def test_always_denied_matches_nothing_and_always_allowed_gives_no_filter():
    nothing = plan_to_filter({"kind": "KIND_ALWAYS_DENIED"})
    assert nothing is not None and not matches(nothing, {"needtoknow:nothing": "x", "team": "eng"})
    assert plan_to_filter({"kind": "KIND_ALWAYS_ALLOWED"}) is None


# ---- a scripted PDP ------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload))
        status, body = self.server.responder(self.path, payload)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.requests = []
    httpd.responder = lambda path, payload: (500, {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def _effects(allow):
    """A CheckResources responder allowing the resources whose attribute `team` is in `allow`."""

    def respond(path, payload):
        results = []
        for item in payload["resources"]:
            resource = item["resource"]
            ok = resource["attr"].get("team") in allow
            effects = {a: "EFFECT_ALLOW" if ok else "EFFECT_DENY" for a in item["actions"]}
            results.append({"resource": {"id": resource["id"], "kind": resource["kind"]}, "actions": effects})
        return 200, {"results": results}

    return respond


HITS = [Hit(f"c{i}", "t", 1.0, {"team": team}) for i, team in enumerate(["eng", "hr", "eng"])]


def test_check_sends_the_principal_and_chunks_as_resources(server):
    server.responder = _effects({"eng"})
    authz = CerbosAuthorizer(server.url, roles=lambda s: ["employee"], policy_version="v2", scope="acme")
    assert authz.check(ANN, HITS) == [True, False, True]
    path, payload = server.requests[0]
    assert path == "/api/check/resources"
    assert payload["principal"] == {
        "id": "ann",
        "roles": ["employee"],
        "attr": {"groups": ["eng"], "region": "eu"},
        "policyVersion": "v2",
        "scope": "acme",
    }
    first = payload["resources"][0]
    assert first == {
        "resource": {"kind": "document", "id": "c0", "attr": {"team": "eng"}, "policyVersion": "v2", "scope": "acme"},
        "actions": ["read"],
    }


def test_check_batches_and_uses_the_resource_field(server):
    server.responder = _effects({"eng"})
    hits = [Hit(f"c{i}", "t", 1.0, {"team": "eng", "doc_id": f"d{i}"}) for i in range(5)]
    authz = CerbosAuthorizer(server.url, batch_size=2, resource_field="doc_id")
    assert authz.check(ANN, hits) == [True] * 5
    assert [len(p["resources"]) for _, p in server.requests] == [2, 2, 1]
    assert server.requests[0][1]["resources"][0]["resource"]["id"] == "d0"


@pytest.mark.parametrize(
    "answer",
    [
        {"results": []},
        {"results": [{"resource": {"id": "someone-else"}, "actions": {"read": "EFFECT_ALLOW"}}]},
        {"results": [{"resource": {"id": "c0"}, "actions": {}}]},
        {"results": [{"resource": {"id": "c0"}, "actions": {"read": "EFFECT_MAYBE"}}]},
        {"nothing": True},
    ],
)
def test_unexpected_answers_fail_closed(server, answer):
    server.responder = lambda path, payload: (200, answer)
    with pytest.raises(AuthorizationError):
        CerbosAuthorizer(server.url).check(ANN, HITS[:1])


def test_filter_for_asks_for_a_plan(server):
    plan = conditional(expr("hasIntersection", var("allowed_groups"), val(["eng"])))
    server.responder = lambda path, payload: (200, {"filter": plan})
    assert CerbosAuthorizer(server.url).filter_for(ANN) == In("allowed_groups", ["eng"])
    path, payload = server.requests[0]
    assert path == "/api/plan/resources"
    assert payload["action"] == "read" and payload["resource"] == {"kind": "document"}
    server.responder = lambda path, payload: (200, {"filter": {"kind": "KIND_SOMETHING"}})
    with pytest.raises(AuthorizationError):
        CerbosAuthorizer(server.url).filter_for(ANN)


def test_configuration_errors():
    with pytest.raises(ValueError):
        CerbosAuthorizer("ftp://cerbos")
    with pytest.raises(ValueError):
        CerbosAuthorizer("http://127.0.0.1:1", batch_size=0)
    with pytest.raises(AuthorizationError, match="action"):
        CerbosAuthorizer("http://127.0.0.1:1").check(ANN, HITS, action="delete")
    with pytest.raises(AuthorizationError, match="role"):
        CerbosAuthorizer("http://127.0.0.1:1", roles=[]).check(ANN, HITS)
    assert CerbosAuthorizer("http://127.0.0.1:1").check(ANN, []) == []


# ---- a real Cerbos running examples/cerbos/policies -----------------------------------------

CERBOS_URL = os.environ.get("NEEDTOKNOW_TEST_CERBOS_URL")
needs_cerbos = pytest.mark.skipif(not CERBOS_URL, reason="set NEEDTOKNOW_TEST_CERBOS_URL to run against Cerbos")


@needs_cerbos
def test_the_example_policy_gives_a_pre_filter():
    # The policy is written in the translatable subset, so retrieval can pre-filter.
    f = CerbosAuthorizer(CERBOS_URL).filter_for(Subject("alice", ["eng"]))
    assert f is not None
    assert matches(f, {"allowed_groups": ["eng"]}) and matches(f, {"allowed_users": ["alice"]})
    assert matches(f, {"allowed_groups": ["*"]}) and not matches(f, {"allowed_groups": ["hr"]})


@needs_cerbos
@settings(max_examples=60, deadline=None)
@given(
    docs=corpora(max_size=15),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_the_example_policy_gives_exactly_the_reference_answer(docs, subject, query, k, strategy):
    authz = CerbosAuthorizer(CERBOS_URL)
    retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + 1)
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


@needs_cerbos
def test_the_example_policy_passes_the_conformance_kit():
    check_authorizer(CerbosAuthorizer(CERBOS_URL))
