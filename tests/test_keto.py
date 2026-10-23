"""KetoAuthorizer: a scripted Keto for every failure, and a real Keto when one is available."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from needtoknow import AuthorizationError, Hit, Subject
from needtoknow.authorizers.keto import KetoAuthorizer
from needtoknow.testing import check_authorizer

ANN = Subject("ann", ["eng", "ops"])


def hit(id_, doc=None):
    return Hit(id_, "text", 1.0, {"doc_id": doc} if doc else {})


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload))
        status, body = self.server.responder(payload)
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
    httpd.responder = lambda payload: (500, {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def granting(allowed):
    """A batch-check responder allowing tuples whose (object, subject) pair is in `allowed`."""

    def respond(payload):
        results = []
        for t in payload["tuples"]:
            who = t.get("subject_id") or "{namespace}:{object}#{relation}".format(**t["subject_set"])
            results.append({"allowed": (t["object"], who) in allowed})
        return 200, {"results": results}

    return respond


def test_each_chunk_is_checked_for_the_user_and_denied_without_its_object(server):
    server.responder = granting({("plan", "ann")})
    authz = KetoAuthorizer(server.url)
    assert authz.check(ANN, [hit("c1", "plan"), hit("c2", "pay"), hit("c3")]) == [True, False, False]
    [(path, payload)] = server.requests
    assert path == "/relation-tuples/batch/check"
    assert payload["tuples"] == [
        {"namespace": "Document", "object": "plan", "relation": "view", "subject_id": "ann"},
        {"namespace": "Document", "object": "pay", "relation": "view", "subject_id": "ann"},
    ]


def test_group_subjects_allow_a_chunk_granted_to_one_of_the_groups(server):
    server.responder = granting({("pay", "Group:ops#members")})
    authz = KetoAuthorizer(server.url, group_subjects=True, resource_field=None, batch_size=2)
    assert authz.check(ANN, [hit("plan"), hit("pay")]) == [False, True]
    assert len(server.requests) == 3  # six tuples (user, eng, ops for each chunk) in batches of two


def test_no_chunks_and_no_objects_mean_no_request(server):
    authz = KetoAuthorizer(server.url)
    assert authz.check(ANN, []) == [] and authz.check(ANN, [hit("c1")]) == [False]
    assert server.requests == []


@pytest.mark.parametrize(
    "answer",
    [
        (500, {}),
        (200, {"results": []}),
        (200, {"results": "yes"}),
        (200, [True]),
        (200, {"results": [{"allowed": "true"}]}),
        (200, {"results": [{}]}),
        (200, {"results": [7]}),
        (200, {"results": [{"allowed": False, "error": "namespace not found"}]}),
    ],
)
def test_a_failed_or_malformed_answer_raises(server, answer):
    server.responder = lambda payload: answer
    with pytest.raises(AuthorizationError):
        KetoAuthorizer(server.url, retries=0).check(ANN, [hit("c1", "plan")])


def test_actions_map_to_relations_and_there_is_no_pre_filter(server):
    server.responder = granting({("plan", "ann")})
    authz = KetoAuthorizer(server.url, actions={"read": "view", "edit": "editors"})
    assert authz.filter_for(ANN) is None and authz.filter_for(ANN, "edit") is None
    authz.check(ANN, [hit("c1", "plan")], action="edit")
    assert server.requests[-1][1]["tuples"][0]["relation"] == "editors"
    with pytest.raises(AuthorizationError, match="delete"):
        authz.check(ANN, [hit("c1", "plan")], action="delete")
    with pytest.raises(AuthorizationError, match="delete"):
        authz.filter_for(ANN, action="delete")


def test_batch_size_must_be_positive():
    with pytest.raises(ValueError):
        KetoAuthorizer("http://127.0.0.1:4466", batch_size=0)


# ---- a real Keto (CI starts one with examples/keto/keto.yml) ---------------------------------

KETO_URL = os.environ.get("NEEDTOKNOW_TEST_KETO_URL")
KETO_WRITE_URL = os.environ.get("NEEDTOKNOW_TEST_KETO_WRITE_URL")
needs_keto = pytest.mark.skipif(not (KETO_URL and KETO_WRITE_URL), reason="set NEEDTOKNOW_TEST_KETO_URL to run")


def write(**relationship):
    request = urllib.request.Request(  # noqa: S310 - the test server's URL
        f"{KETO_WRITE_URL}/admin/relation-tuples",
        data=json.dumps(relationship).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10):  # noqa: S310 - the test server's URL
        pass


@pytest.fixture(scope="module")
def keto():
    write(namespace="Document", object="ntk-plan", relation="view", subject_id="ann")
    write(
        namespace="Document",
        object="ntk-pay",
        relation="view",
        subject_set={"namespace": "Group", "object": "ntk-hr", "relation": "members"},
    )
    write(namespace="Group", object="ntk-hr", relation="members", subject_id="hana")
    return KETO_URL


@needs_keto
def test_a_real_keto_follows_direct_and_group_relationships(keto):
    authz = KetoAuthorizer(keto)
    hits = [hit("c1", "ntk-plan"), hit("c2", "ntk-pay"), hit("c3", "ntk-none")]
    assert authz.check(Subject("ann"), hits) == [True, False, False]
    assert authz.check(Subject("hana"), hits) == [False, True, False]  # through Group:ntk-hr#members
    grouped = KetoAuthorizer(keto, group_subjects=True)
    assert grouped.check(Subject("zed", ["ntk-hr"]), hits) == [False, True, False]  # groups from the token


@needs_keto
def test_a_real_keto_passes_the_conformance_kit(keto):
    check_authorizer(KetoAuthorizer(keto))
