"""OPAAuthorizer against a scripted HTTP server, and against a real OPA when one is available."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import AuthorizationError, Document, Hit, In, InMemoryBackend, Or, PermissionedRetriever, Subject
from wardowl.authorizers.opa import OPAAuthorizer
from wardowl.testing import check_authorizer

BOB = Subject("bob", ["eng"], {"region": "eu"})
HITS = [Hit("a", "text a", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "text b", 0.5, {"allowed_groups": ["hr"]})]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload))
        status, body = self.server.responder(self.path, payload)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        # A client that timed out has already closed the socket; that is expected in these tests.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []
    srv.responder = lambda path, payload: (200, {})
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def test_check_sends_subject_action_and_resources_and_reads_decisions(server):
    server.responder = lambda path, payload: (200, {"result": [True, False]})
    assert OPAAuthorizer(server.url).check(BOB, HITS) == [True, False]
    path, payload = server.requests[0]
    assert path == "/v1/data/wardowl/decisions"
    assert payload["input"] == {
        "subject": {"id": "bob", "groups": ["eng"], "attributes": {"region": "eu"}},
        "action": "read",
        "resources": [
            {"id": "a", "metadata": {"allowed_groups": ["eng"]}},
            {"id": "b", "metadata": {"allowed_groups": ["hr"]}},
        ],
    }


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {}),  # undefined decision
        (200, {"result": True}),  # not a list
        (200, {"result": [True]}),  # wrong length
        (200, {"result": [True, "yes"]}),  # not booleans
        (200, b"not json"),
        (500, {"error": "boom"}),
        (404, {"result": [True, True]}),
    ],
)
def test_bad_answers_fail_closed(server, status, body):
    server.responder = lambda path, payload: (status, body)
    with pytest.raises(AuthorizationError):
        OPAAuthorizer(server.url).check(BOB, HITS)


def test_slow_and_unreachable_servers_fail_closed(server):
    def slow(path, payload):
        time.sleep(0.5)
        return 200, {"result": [True, True]}

    server.responder = slow
    with pytest.raises(AuthorizationError):
        OPAAuthorizer(server.url, timeout=0.1).check(BOB, HITS)
    with pytest.raises(AuthorizationError):
        OPAAuthorizer("http://127.0.0.1:1", timeout=0.5).check(BOB, HITS)


def test_filter_documents_are_parsed_and_validated(server):
    authz = OPAAuthorizer(server.url, filter_path="wardowl/filter")
    server.responder = lambda path, payload: (
        200,
        {"result": {"or": [{"in": {"field": "allowed_groups", "values": ["eng", "*"]}}]}},
    )
    assert authz.filter_for(BOB) == Or(In("allowed_groups", {"eng", "*"}))
    assert server.requests[-1] == (
        "/v1/data/wardowl/filter",
        {"input": {"subject": {"id": "bob", "groups": ["eng"], "attributes": {"region": "eu"}}, "action": "read"}},
    )
    server.responder = lambda path, payload: (200, {"result": None})
    assert authz.filter_for(BOB) is None
    server.responder = lambda path, payload: (200, {"result": {"in": {"field": "x", "values": [True]}}})
    with pytest.raises(AuthorizationError):
        authz.filter_for(BOB)


def test_without_a_filter_path_no_filter_is_requested(server):
    assert OPAAuthorizer(server.url).filter_for(BOB) is None
    assert server.requests == []


def test_configuration_and_input_errors():
    with pytest.raises(ValueError):
        OPAAuthorizer("file:///etc/passwd")
    with pytest.raises(AuthorizationError):
        OPAAuthorizer("http://127.0.0.1:1").check(BOB, [Hit("a", "t", 1.0, {"blob": object()})])
    assert OPAAuthorizer("http://127.0.0.1:1").check(BOB, []) == []


def test_retriever_fails_closed_when_opa_is_down():
    backend = InMemoryBackend([Document("handbook", "leave policy", {"allowed_groups": ["*"]})])
    retriever = PermissionedRetriever(backend, OPAAuthorizer("http://127.0.0.1:1", timeout=0.5))
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)


# ---- against a real OPA running examples/opa/wardowl.rego ----------------------------------

OPA_URL = os.environ.get("WARDOWL_TEST_OPA_URL")


@pytest.mark.skipif(not OPA_URL, reason="set WARDOWL_TEST_OPA_URL to run against a real OPA")
@settings(max_examples=60, deadline=None)
@given(
    docs=corpora(max_size=15),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_example_policy_gives_exactly_the_reference_answer(docs, subject, query, k, strategy):
    authz = OPAAuthorizer(OPA_URL, filter_path="wardowl/filter")
    retriever = PermissionedRetriever(InMemoryBackend(docs), authz, strategy=strategy, max_candidates=len(docs) + k)
    result = retriever.retrieve(query, subject, k)
    assert [h.id for h in result.hits] == oracle_top_k(docs, subject, query, k)


@pytest.mark.skipif(not OPA_URL, reason="set WARDOWL_TEST_OPA_URL to run against a real OPA")
def test_example_policy_passes_the_conformance_kit():
    # Its decisions and its filter are written separately in Rego; they must agree for every hit.
    check_authorizer(OPAAuthorizer(OPA_URL, filter_path="wardowl/filter"))
