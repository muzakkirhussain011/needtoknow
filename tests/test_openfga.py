"""OpenFGAAuthorizer against a scripted HTTP server, and against a real OpenFGA when available."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st
from test_opa import _Handler

from needtoknow import AuthorizationError, Document, Hit, In, InMemoryBackend, PermissionedRetriever, Subject
from needtoknow.authorizers.openfga import OpenFGAAuthorizer

BOB = Subject("bob", ["eng"])


@pytest.fixture
def fga():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []
    srv.responder = lambda path, payload: (200, {})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def _all_allowed(path, payload):
    return 200, {"result": {c["correlation_id"]: {"allowed": True} for c in payload["checks"]}}


def test_batch_check_dedupes_resources_batches_requests_and_sends_groups(fga):
    fga.responder = _all_allowed
    hits = [Hit(f"c{i}", "t", 1.0, {"doc_id": d}) for i, d in enumerate(["a", "b", "a", "c"])]
    authz = OpenFGAAuthorizer(fga.url, "store1", authorization_model_id="m1", batch_size=2)
    assert authz.check(BOB, hits) == [True, True, True, True]
    assert [p for p, _ in fga.requests] == ["/stores/store1/batch-check"] * 2
    first = fga.requests[0][1]
    assert first["authorization_model_id"] == "m1"
    assert first["checks"][0]["tuple_key"] == {"user": "user:bob", "relation": "viewer", "object": "document:a"}
    assert first["checks"][0]["contextual_tuples"] == {
        "tuple_keys": [{"user": "user:bob", "relation": "member", "object": "group:eng"}]
    }


def test_chunks_without_a_resource_id_are_denied_without_asking(fga):
    authz = OpenFGAAuthorizer(fga.url, "s")
    assert authz.check(BOB, [Hit("c1", "t", 1.0, {})]) == [False]
    assert fga.requests == []


@pytest.mark.parametrize(
    "result",
    [None, {}, {"0": {"error": {"message": "boom"}}}, {"0": {"allowed": "yes"}}],
)
def test_undecided_or_malformed_checks_fail_closed(fga, result):
    fga.responder = lambda path, payload: (200, {} if result is None else {"result": result})
    with pytest.raises(AuthorizationError):
        OpenFGAAuthorizer(fga.url, "s").check(BOB, [Hit("c1", "t", 1.0, {"doc_id": "a"})])


def test_http_errors_and_unknown_actions_fail_closed(fga):
    fga.responder = lambda path, payload: (500, {"code": "internal_error"})
    hit = Hit("c1", "t", 1.0, {"doc_id": "a"})
    with pytest.raises(AuthorizationError):
        OpenFGAAuthorizer(fga.url, "s").check(BOB, [hit])
    with pytest.raises(AuthorizationError):
        OpenFGAAuthorizer(fga.url, "s").check(BOB, [hit], action="delete")


def test_list_objects_becomes_a_filter_unless_it_may_be_truncated(fga):
    fga.responder = lambda path, payload: (200, {"objects": ["document:a", "document:b"]})
    assert OpenFGAAuthorizer(fga.url, "s").filter_for(BOB) == In("doc_id", {"a", "b"})
    assert OpenFGAAuthorizer(fga.url, "s", list_objects_limit=2).filter_for(BOB) is None
    assert OpenFGAAuthorizer(fga.url, "s", resource_field=None).filter_for(BOB) is None


# ---- against a real OpenFGA ------------------------------------------------------------------

FGA_URL = os.environ.get("NEEDTOKNOW_TEST_OPENFGA_URL")

MODEL = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "user"},
        {
            "type": "group",
            "relations": {"member": {"this": {}}},
            "metadata": {"relations": {"member": {"directly_related_user_types": [{"type": "user"}]}}},
        },
        {
            "type": "document",
            "relations": {"viewer": {"this": {}}},
            "metadata": {
                "relations": {
                    "viewer": {
                        "directly_related_user_types": [
                            {"type": "user"},
                            {"type": "user", "wildcard": {}},
                            {"type": "group", "relation": "member"},
                        ]
                    }
                }
            },
        },
    ],
}


def _api(path, payload):
    request = urllib.request.Request(  # noqa: S310 - test helper, URL from the test environment
        f"{FGA_URL}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read())


def _store_for(docs):
    store = _api("/stores", {"name": f"ntk-{uuid.uuid4().hex[:12]}"})["id"]
    model = _api(f"/stores/{store}/authorization-models", MODEL)["authorization_model_id"]
    tuples = []
    for d in docs:
        obj = f"document:{d.id}"
        tuples += [
            {"user": f"user:{u}", "relation": "viewer", "object": obj} for u in d.metadata.get("allowed_users", [])
        ]
        for g in d.metadata.get("allowed_groups", []):
            user = "user:*" if g == "*" else f"group:{g}#member"
            tuples.append({"user": user, "relation": "viewer", "object": obj})
    for i in range(0, len(tuples), 100):
        _api(f"/stores/{store}/write", {"writes": {"tuple_keys": tuples[i : i + 100]}, "authorization_model_id": model})
    return store, model


@pytest.mark.skipif(not FGA_URL, reason="set NEEDTOKNOW_TEST_OPENFGA_URL to run against a real OpenFGA")
@settings(max_examples=40, deadline=None)
@given(
    docs=corpora(max_size=12),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_openfga_gives_exactly_the_reference_answer(docs, subject, query, k, strategy):
    store, model = _store_for(docs)
    stored = [Document(d.id, d.text, {**d.metadata, "doc_id": d.id}) for d in docs]
    authz = OpenFGAAuthorizer(FGA_URL, store, authorization_model_id=model)
    retriever = PermissionedRetriever(InMemoryBackend(stored), authz, strategy=strategy, max_candidates=len(docs) + 1)
    result = retriever.retrieve(query, subject, k)
    assert [h.id for h in result.hits] == oracle_top_k(docs, subject, query, k)
