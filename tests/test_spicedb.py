"""SpiceDBAuthorizer against a scripted HTTP server, and against a real SpiceDB when one is available."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import AuthorizationError, Document, Hit, In, InMemoryBackend, PermissionedRetriever, Subject
from wardowl.authorizers.spicedb import SpiceDBAuthorizer
from wardowl.testing import check_authorizer

ANN = Subject("ann", ["eng"])
HITS = [Hit("c1", "t", 1.0, {"doc_id": "d1"}), Hit("c2", "t", 1.0, {"doc_id": "d2"}), Hit("c3", "t", 1.0, {})]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload, dict(self.headers)))
        status, body = self.server.responder(self.path, payload)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
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


def _bulk(allowed):
    """A CheckBulkPermissions responder granting the (document, subject) pairs in `allowed`."""

    def respond(path, payload):
        pairs = []
        for item in payload["items"]:
            subject = item["subject"]
            who = subject["object"]["objectId"]
            if "optionalRelation" in subject:
                who += "#" + subject["optionalRelation"]
            ok = (item["resource"]["objectId"], who) in allowed
            permissionship = "PERMISSIONSHIP_HAS_PERMISSION" if ok else "PERMISSIONSHIP_NO_PERMISSION"
            pairs.append({"request": item, "item": {"permissionship": permissionship}})
        return 200, {"pairs": list(reversed(pairs))}  # out of order: answers are matched by request

    return respond


def _lookup(*lines):
    return lambda path, payload: (200, b"\n".join(json.dumps(line).encode() for line in lines) + b"\n")


# ---- scripted server -------------------------------------------------------------------------


def test_check_asks_for_the_user_and_each_group_and_matches_answers_by_request(server):
    server.responder = _bulk({("d1", "eng#member")})
    authz = SpiceDBAuthorizer(server.url, "secret", consistency={"fullyConsistent": True})
    assert authz.check(ANN, HITS) == [True, False, False]  # c3 has no doc_id: denied without asking
    path, payload, headers = server.requests[0]
    assert path == "/v1/permissions/checkbulk" and headers["Authorization"] == "Bearer secret"
    assert payload["consistency"] == {"fullyConsistent": True}
    user = {"object": {"objectType": "user", "objectId": "ann"}}
    group = {"object": {"objectType": "group", "objectId": "eng"}, "optionalRelation": "member"}
    asked = {json.dumps(i["subject"], sort_keys=True) for i in payload["items"]}
    assert asked == {json.dumps(user, sort_keys=True), json.dumps(group, sort_keys=True)}
    assert {i["resource"]["objectId"] for i in payload["items"]} == {"d1", "d2"}


def test_group_subjects_can_be_turned_off_and_batches_are_split(server):
    server.responder = _bulk({("d2", "ann")})
    authz = SpiceDBAuthorizer(server.url, group_subjects=False, batch_size=1)
    assert authz.check(ANN, HITS) == [False, True, False]
    assert len(server.requests) == 2 and all(len(p["items"]) == 1 for _, p, _ in server.requests)


@pytest.mark.parametrize(
    "answer",
    [
        {"pairs": [{"request": {}, "error": {"code": 3, "message": "bad id"}}]},
        {"pairs": "nope"},
        {"pairs": []},  # a question with no answer
        {"pairs": [{"item": {"permissionship": "PERMISSIONSHIP_HAS_PERMISSION"}}]},  # no request echoed
    ],
)
def test_malformed_and_failed_bulk_answers_fail_closed(server, answer):
    server.responder = lambda path, payload: (200, answer)
    with pytest.raises(AuthorizationError):
        SpiceDBAuthorizer(server.url, group_subjects=False).check(ANN, HITS[:1])


def test_a_conditional_answer_is_not_a_decision(server):
    def conditional(path, payload):
        item = payload["items"][0]
        return 200, {"pairs": [{"request": item, "item": {"permissionship": "PERMISSIONSHIP_CONDITIONAL_PERMISSION"}}]}

    server.responder = conditional
    with pytest.raises(AuthorizationError, match="caveat"):
        SpiceDBAuthorizer(server.url, group_subjects=False).check(ANN, HITS[:1])


def test_caveat_context_is_sent_with_every_question(server):
    server.responder = _bulk(set())
    authz = SpiceDBAuthorizer(server.url, group_subjects=False, caveat_context=lambda s: {"region": "eu"})
    authz.check(ANN, HITS[:1])
    assert server.requests[0][1]["items"][0]["context"] == {"region": "eu"}
    broken = SpiceDBAuthorizer(server.url, caveat_context=lambda s: 1 / 0)
    with pytest.raises(AuthorizationError, match="caveat_context"):
        broken.check(ANN, HITS[:1])


def test_filter_for_reads_the_lookup_stream(server):
    has = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
    server.responder = _lookup({"result": {"resourceObjectId": "d1", "permissionship": has}})
    assert SpiceDBAuthorizer(server.url).filter_for(ANN) == In("doc_id", ["d1"])
    # One lookup for the user and one for each group, merged.
    assert [p["subject"]["object"]["objectId"] for _, p, _ in server.requests] == ["ann", "eng"]
    assert server.requests[0][1]["optionalLimit"] == 1000


def test_a_possibly_truncated_or_conditional_lookup_gives_no_filter(server):
    has = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
    server.responder = _lookup(*({"result": {"resourceObjectId": f"d{i}", "permissionship": has}} for i in range(3)))
    assert SpiceDBAuthorizer(server.url, lookup_limit=3).filter_for(ANN) is None
    server.responder = _lookup(
        {"result": {"resourceObjectId": "d1", "permissionship": "LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION"}}
    )
    assert SpiceDBAuthorizer(server.url).filter_for(ANN) is None


@pytest.mark.parametrize(
    "line",
    [{"error": {"code": 5, "message": "not found"}}, {"result": {"permissionship": "x"}}, ["not", "an", "object"]],
)
def test_lookup_errors_fail_closed(server, line):
    server.responder = _lookup(line)
    with pytest.raises(AuthorizationError):
        SpiceDBAuthorizer(server.url).filter_for(ANN)


def test_without_a_resource_field_chunks_are_resources_and_there_is_no_filter(server):
    server.responder = _bulk({("c2", "ann")})
    authz = SpiceDBAuthorizer(server.url, resource_field=None, group_subjects=False)
    assert authz.check(ANN, HITS) == [False, True, False]
    assert authz.filter_for(ANN) is None


def test_configuration_errors():
    with pytest.raises(ValueError):
        SpiceDBAuthorizer("ftp://spicedb")
    with pytest.raises(ValueError):
        SpiceDBAuthorizer("http://127.0.0.1:1", batch_size=0)
    with pytest.raises(AuthorizationError, match="action"):
        SpiceDBAuthorizer("http://127.0.0.1:1").check(ANN, HITS, action="delete")
    assert SpiceDBAuthorizer("http://127.0.0.1:1").check(ANN, []) == []


def test_an_unreachable_spicedb_fails_closed():
    retriever = PermissionedRetriever(
        InMemoryBackend([Document("c1", "leave", {"doc_id": "d1"})]),
        SpiceDBAuthorizer("http://127.0.0.1:1", timeout=0.5),
    )
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", ANN)


# ---- a real SpiceDB --------------------------------------------------------------------------

SPICEDB_URL = os.environ.get("WARDOWL_TEST_SPICEDB_URL")
SPICEDB_TOKEN = os.environ.get("WARDOWL_TEST_SPICEDB_TOKEN", "")
needs_spicedb = pytest.mark.skipif(not SPICEDB_URL, reason="set WARDOWL_TEST_SPICEDB_URL to run against SpiceDB")

SCHEMA = """
definition user {}

definition group {
    relation member: user
}

definition document {
    relation viewer: user | user:* | group#member
    permission view = viewer
}
"""


def _api(path, payload):
    request = urllib.request.Request(  # noqa: S310 - test helper, URL from the test environment
        f"{SPICEDB_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {SPICEDB_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read() or b"{}")


def _relationships_for(docs):
    """Replace every document relationship with those the metadata ACLs describe."""
    _api("/v1/schema/write", {"schema": SCHEMA})
    _api("/v1/relationships/delete", {"relationshipFilter": {"resourceType": "document"}})
    updates = []
    for d in docs:
        resource = {"objectType": "document", "objectId": d.id}
        subjects_ = [{"object": {"objectType": "user", "objectId": u}} for u in d.metadata.get("allowed_users", [])]
        for g in d.metadata.get("allowed_groups", []):
            if g == "*":
                subjects_.append({"object": {"objectType": "user", "objectId": "*"}})
            else:
                subjects_.append({"object": {"objectType": "group", "objectId": g}, "optionalRelation": "member"})
        updates += [
            {"operation": "OPERATION_TOUCH", "relationship": {"resource": resource, "relation": "viewer", "subject": s}}
            for s in subjects_
        ]
    for i in range(0, len(updates), 100):
        _api("/v1/relationships/write", {"updates": updates[i : i + 100]})


def _authorizer():
    return SpiceDBAuthorizer(SPICEDB_URL, SPICEDB_TOKEN, consistency={"fullyConsistent": True})


@needs_spicedb
@settings(max_examples=40, deadline=None)
@given(
    docs=corpora(max_size=12),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_spicedb_gives_exactly_the_reference_answer(docs, subject, query, k, strategy):
    _relationships_for(docs)
    stored = [Document(d.id, d.text, {**d.metadata, "doc_id": d.id}) for d in docs]
    retriever = PermissionedRetriever(
        InMemoryBackend(stored), _authorizer(), strategy=strategy, max_candidates=len(docs) + 1
    )
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


@needs_spicedb
def test_spicedb_passes_the_conformance_kit():
    docs = [
        Document("doc-a", "t", {"allowed_users": ["alice"]}),
        Document("doc-b", "t", {"allowed_groups": ["hr"]}),
        Document("doc-c", "t", {"allowed_groups": ["*"]}),
        Document("doc-d", "t", {}),
    ]
    _relationships_for(docs)
    hits = [Hit(d.id, d.text, 1.0, {**d.metadata, "doc_id": d.id}) for d in docs]
    people = [Subject("alice"), Subject("bob", ["hr"]), Subject("carol", ["eng", "hr"]), Subject("dave")]
    check_authorizer(_authorizer(), people, hits)
