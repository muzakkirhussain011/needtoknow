"""The authorizers' HTTP client under injected faults, against a real keep-alive HTTP server."""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wardowl import AuthorizationError, Hit, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from wardowl.authorizers import CircuitBreaker, _http
from wardowl.authorizers._http import JsonClient
from wardowl.authorizers.opa import OPAAuthorizer
from wardowl.authorizers.spicedb import SpiceDBAuthorizer

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
        answer = {"result": _decisions(payload)}
        if action.startswith("big"):
            answer["padding"] = "x" * 2000
        body = b"not json" if action == "badjson" else json.dumps(answer).encode()
        if action == "drip":  # a correct answer, status line and headers included, one byte at a time
            self.close_connection = True
            head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            with contextlib.suppress(OSError):  # until the client gives up
                for byte in head.encode() + body:
                    self.wfile.write(bytes([byte]))
                    time.sleep(0.02)
            return
        if action == "big-chunked":  # no length announced: the size shows only while reading
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in (body[:1000], body[1000:]):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                self.wfile.write(b"0\r\n\r\n")
            return
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
    def client(timeout=0.2):
        c = JsonClient(server.url, service="test", timeout=timeout, headers={}, max_response_bytes=1000)
        c._proxied = True  # as if a proxy applied; without proxy variables urllib connects directly
        return c

    payload = {"input": {"subject": {"id": "bob", "groups": ["eng"]}, "resources": []}}
    assert client(2).post_json("/x", payload, "test") == {"result": []}  # the deadline covers the retry too
    server.script = ["503"]
    assert client(2).post_json("/x", payload, "test") == {"result": []}
    for fault in ["302", "400", "slow", "drip", "big", "big-chunked"]:
        server.script = [fault]
        with pytest.raises(AuthorizationError):
            client().post_json("/x", payload, "test")
    tls = JsonClient(server.url.replace("http://", "https://"), service="test", timeout=1, headers={}, retries=0)
    tls._proxied = True
    with pytest.raises(AuthorizationError):  # the test server speaks no TLS
        tls.post_json("/x", payload, "test")


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
    with pytest.raises(ValueError):
        OPAAuthorizer("http://opa:8181", timeout=0)
    with pytest.raises(ValueError):
        OPAAuthorizer("http://opa:8181", max_response_bytes=0)


def test_timeout_bounds_the_whole_call_not_each_read(server):
    # The answer arrives a byte every 20 ms: each read is quick, the whole would take about 2 s.
    server.script = ["drip"]
    started = time.monotonic()
    with pytest.raises(AuthorizationError, match="timed out"):
        OPAAuthorizer(server.url, timeout=0.3).check(BOB, HITS)
    assert time.monotonic() - started < 1.0
    assert len(server.requests) == 1  # a timeout is not retried


@pytest.mark.parametrize("fault", ["big", "big-chunked"])
def test_an_answer_over_max_response_bytes_is_refused(server, fault):
    server.script = [fault]
    with pytest.raises(AuthorizationError, match="more than 1000 bytes"):
        OPAAuthorizer(server.url, max_response_bytes=1000).check(BOB, HITS)
    assert len(server.requests) == 1  # not retried
    server.script = [fault]
    assert OPAAuthorizer(server.url, max_response_bytes=10_000).check(BOB, HITS) == [True, False]


def test_a_connection_made_before_fork_is_not_reused_after_it(server, monkeypatch):
    authz = OPAAuthorizer(server.url)
    assert authz.check(BOB, HITS) == [True, False]
    monkeypatch.setattr(_http, "os", SimpleNamespace(getpid=lambda: -1))  # as in a forked child
    assert authz.check(BOB, HITS) == [True, False]  # on a connection of its own
    assert authz.check(BOB, HITS) == [True, False]  # which it then keeps
    assert len(server.requests) == 3 and server.requests[0] != server.requests[1] == server.requests[2]


@pytest.mark.skipif(not hasattr(os, "fork") or sys.platform == "darwin", reason="needs fork() (unsafe on macOS)")
def test_a_forked_child_never_shares_the_parents_connection(server):
    # With gunicorn --preload, workers forked after a request would otherwise share one socket and
    # could read each other's permission answers.
    authz = OPAAuthorizer(server.url)
    assert authz.check(BOB, HITS) == [True, False]
    pid = os.fork()
    if pid == 0:  # the child: always leave at once, without running the rest of the tests
        code = 1
        try:
            code = 0 if authz.check(BOB, HITS) == [True, False] else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert authz.check(BOB, HITS) == [True, False]
    assert len(server.requests) == 3 and server.requests[0] == server.requests[2] != server.requests[1]


def test_tls_settings_and_credentials_are_refused_where_they_would_not_protect(monkeypatch):
    for name in ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    tls = ssl.create_default_context()
    with pytest.raises(ValueError, match="ssl_context"):
        OPAAuthorizer("http://opa.internal:8181", ssl_context=tls)  # it would be ignored
    OPAAuthorizer("https://opa.internal:8181", ssl_context=tls)
    token = {"Authorization": "Bearer secret"}
    for insecure in (
        lambda: OPAAuthorizer("http://opa.internal:8181", headers=token),
        lambda: OPAAuthorizer("http://10.0.0.5:8181", headers={"authorization": "Bearer secret"}),
        lambda: SpiceDBAuthorizer("http://spicedb.internal:8443", "secret"),
    ):
        with pytest.raises(ValueError, match="allow_insecure"):
            insecure()
    OPAAuthorizer("http://opa.internal:8181", headers=token, allow_insecure=True)
    SpiceDBAuthorizer("http://spicedb.internal:8443", "secret", allow_insecure=True)
    SpiceDBAuthorizer("https://spicedb.internal:8443", "secret")
    SpiceDBAuthorizer("http://spicedb.internal:8443")  # no token to leak
    for local in ("http://localhost:8181", "http://127.0.0.1:8181", "http://127.8.0.1:8181", "http://[::1]:8181"):
        OPAAuthorizer(local, headers=token)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "")
    with pytest.raises(ValueError, match="allow_insecure"):
        OPAAuthorizer("http://127.0.0.1:8181", headers=token)  # through the proxy, it would leave this machine


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


def test_a_refused_connection_is_kept_as_the_cause_of_the_failure():
    # Monitoring reads the cause (RetrievalEvent.cause) to tell an unreachable service from a bad answer.
    with pytest.raises(AuthorizationError) as failure:
        OPAAuthorizer("http://127.0.0.1:9", retries=0, timeout=2).check(BOB, HITS)
    assert isinstance(failure.value.__cause__, OSError)
