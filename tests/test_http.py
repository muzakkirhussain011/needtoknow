"""The authorizers' HTTP client under injected faults, against a real keep-alive HTTP server."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from needtoknow import AuthorizationError, Hit, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.authorizers import CircuitBreaker
from needtoknow.authorizers._http import JsonClient
from needtoknow.authorizers.opa import OPAAuthorizer

BOB = Subject("bob", ["eng"])
HITS = [Hit("a", "t", 1.0, {"allowed_groups": ["eng"]}), Hit("b", "t", 0.5, {"allowed_groups": ["hr"]})]


def _decisions(payload):
    """What a correct OPA policy answers: the reference rules, applied to the posted input."""
    data = payload["input"]
    subject = Subject(data["subject"]["id"], data["subject"]["groups"])
    hits = [Hit(r["id"], "", 0.0, r["metadata"]) for r in data["resources"]]
    return MetadataACLAuthorizer().check(subject, hits)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep connections alive, like OPA and OpenFGA

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv = self.server
        with srv.lock:
            srv.requests.append(self.client_address[1])  # the client port identifies the connection
            action = srv.script.pop(0) if srv.script else "ok"
        if action == "reset":  # close without answering
            self.close_connection = True
            return
        if action == "slow":
            time.sleep(0.4)
        responses = {"503": (503, {}), "400": (400, {}), "302": (302, {"Location": "/elsewhere"})}
        status, extra = responses.get(action, (200, {}))
        body = b"not json" if action == "badjson" else json.dumps({"result": _decisions(payload)}).encode()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(status)
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        if action == "ok-then-close":  # answer as if keeping the connection, then drop it while idle
            self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.lock = threading.Lock()
    srv.requests, srv.script = [], []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def test_connections_are_kept_alive_and_reused(server):
    authz = OPAAuthorizer(server.url)
    for _ in range(5):
        assert authz.check(BOB, HITS) == [True, False]
    assert len(server.requests) == 5 and len(set(server.requests)) == 1


def test_a_connection_closed_while_idle_is_replaced_without_using_a_retry(server):
    server.script = ["ok-then-close"]
    authz = OPAAuthorizer(server.url, retries=0)
    assert authz.check(BOB, HITS) == [True, False]
    time.sleep(0.05)  # let the server close the idle connection
    assert authz.check(BOB, HITS) == [True, False]
    assert len(set(server.requests)) == 2


@pytest.mark.parametrize("fault", ["503", "reset"])
def test_transient_failures_are_retried_once(server, fault):
    server.script = [fault]
    assert OPAAuthorizer(server.url).check(BOB, HITS) == [True, False]
    assert len(server.requests) == 2
    server.requests.clear()
    server.script = [fault, fault]
    with pytest.raises(AuthorizationError):
        OPAAuthorizer(server.url).check(BOB, HITS)
    assert len(server.requests) == 2
    server.requests.clear()
    server.script = [fault]
    with pytest.raises(AuthorizationError):
        OPAAuthorizer(server.url, retries=0).check(BOB, HITS)
    assert len(server.requests) == 1


@pytest.mark.parametrize("fault", ["slow", "400", "badjson", "302"])
def test_timeouts_client_errors_bad_answers_and_redirects_are_not_retried(server, fault):
    server.script = [fault]
    with pytest.raises(AuthorizationError):
        OPAAuthorizer(server.url, timeout=0.2).check(BOB, HITS)
    assert len(server.requests) == 1


def test_the_urllib_path_used_behind_a_proxy_behaves_the_same(server):
    def client():
        c = JsonClient(server.url, service="test", timeout=0.2, headers={})
        c._proxied = True  # as if a proxy applied; without proxy variables urllib connects directly
        return c

    payload = {"input": {"subject": {"id": "bob", "groups": ["eng"]}, "resources": []}}
    assert client().post_json("/x", payload, "test") == {"result": []}
    server.script = ["503"]
    assert client().post_json("/x", payload, "test") == {"result": []}
    for fault in ["302", "400", "slow"]:
        server.script = [fault]
        with pytest.raises(AuthorizationError):
            client().post_json("/x", payload, "test")


def test_proxy_variables_decide_whether_urllib_is_used(monkeypatch):
    for name in ("http_proxy", "https_proxy", "no_proxy"):  # Python prefers the lowercase ones
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "")
    assert JsonClient("http://opa.internal:8181", service="t", timeout=1, headers={})._proxied
    monkeypatch.setenv("NO_PROXY", "opa.internal")
    assert not JsonClient("http://opa.internal:8181", service="t", timeout=1, headers={})._proxied


def test_the_circuit_breaker_pauses_requests_then_probes(server):
    now = [0.0]
    breaker = CircuitBreaker(failures=2, reset_after=10, clock=lambda: now[0])
    authz = OPAAuthorizer(server.url, retries=0, circuit_breaker=breaker)
    server.script = ["503", "503"]
    for _ in range(2):
        with pytest.raises(AuthorizationError):
            authz.check(BOB, HITS)
    assert breaker.is_open and len(server.requests) == 2
    with pytest.raises(AuthorizationError):  # paused: the server is not contacted
        authz.check(BOB, HITS)
    assert len(server.requests) == 2
    now[0] = 10.0
    server.script = ["503"]
    with pytest.raises(AuthorizationError):  # the probe fails, so the breaker opens again at once
        authz.check(BOB, HITS)
    assert breaker.is_open and len(server.requests) == 3
    now[0] = 20.0
    assert authz.check(BOB, HITS) == [True, False]  # the probe succeeds and closes the breaker
    assert not breaker.is_open


def test_client_errors_do_not_open_the_breaker(server):
    breaker = CircuitBreaker(failures=1)
    authz = OPAAuthorizer(server.url, circuit_breaker=breaker)
    server.script = ["400", "badjson"]
    for _ in range(2):
        with pytest.raises(AuthorizationError):
            authz.check(BOB, HITS)
    assert not breaker.is_open


def test_breaker_and_client_validate_their_settings():
    with pytest.raises(ValueError):
        CircuitBreaker(failures=0)
    with pytest.raises(ValueError):
        OPAAuthorizer("http://opa:8181", retries=-1)
    with pytest.raises(ValueError):
        OPAAuthorizer("https://")


FAULTS = st.sampled_from(["ok", "503", "reset", "ok-then-close", "400", "badjson", "302"])


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    docs=corpora(max_size=12),
    subject=subjects,
    query=queries,
    k=st.integers(1, 5),
    faults=st.lists(FAULTS, max_size=6),
)
def test_under_any_faults_retrieval_is_exact_or_fails_closed(server, docs, subject, query, k, faults):
    server.script = list(faults)
    retriever = PermissionedRetriever(
        InMemoryBackend(docs), OPAAuthorizer(server.url), strategy="postfilter", max_candidates=len(docs) + 1
    )
    try:
        result = retriever.retrieve(query, subject, k)
    except AuthorizationError:
        return
    assert [h.id for h in result.hits] == oracle_top_k(docs, subject, query, k)
