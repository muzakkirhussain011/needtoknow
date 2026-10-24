"""Pinecone: filter translation, requests and replies, waiting for writes, and the conformance kit on Pinecone."""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest

from wardowl import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from wardowl.backends._engines import tag
from wardowl.backends.pinecone import TERMS_FIELD, PineconeBackend, filter_to_pinecone, record_id
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
KEY = os.environ.get("WARDOWL_TEST_PINECONE_API_KEY")
INDEX = "wardowl-ci"


def _h(field, value):
    return tag(field, value).encode().hex()


# ---- translation and ids (no service) ----------------------------------------------------------


def test_filters_become_in_conditions_on_hex_terms():
    assert filter_to_pinecone(In("team", ["b", 1])) == {TERMS_FIELD: {"$in": [_h("team", 1), _h("team", "b")]}}
    a, b, c = ({TERMS_FIELD: {"$in": [_h(f, v)]}} for f, v in [("a", "x"), ("b", "y"), ("c", "z")])
    assert filter_to_pinecone(And(In("a", ["x"]), Or(In("b", ["y"]), In("c", ["z"])))) == {"$and": [a, {"$or": [b, c]}]}
    assert filter_to_pinecone(And(In("a", ["x"]))) == a
    assert filter_to_pinecone(In("a", [])) is None
    assert filter_to_pinecone(And(In("a", ["x"]), In("b", []))) is None
    assert filter_to_pinecone(Or(In("a", []), In("b", ["y"]))) == b
    assert filter_to_pinecone(Or(In("a", []))) is None
    wide = filter_to_pinecone(In("g", range(10_001)))
    assert [len(p[TERMS_FIELD]["$in"]) for p in wide["$or"]] == [10_000, 1]
    with pytest.raises(ValueError):
        filter_to_pinecone(In("a\x1fb", ["x"]))
    with pytest.raises(TypeError):
        filter_to_pinecone("a")


def test_record_ids_are_printable_ascii_up_to_512_characters():
    assert record_id("doc-1") == "doc-1"
    for odd in ("café", "x" * 513, "tab\there", ""):
        assert record_id(odd).startswith("sha256-") and len(record_id(odd)) == 71
    assert record_id("café") != record_id("cafe")


# ---- requests and replies (a fake Pinecone) ----------------------------------------------------


class Fake:
    def __init__(self, replies=None, status=200):
        httpx = pytest.importorskip("httpx")
        self.requests, self.replies, self.status = [], list(replies or []), status
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        import httpx

        self.requests.append(request)
        if request.url.path == "/describe_index_stats":
            return httpx.Response(200, json={"dimension": EMBED.dim})
        if request.url.path == "/query":
            return httpx.Response(self.status, json=self.replies.pop(0) if self.replies else {"matches": []})
        return httpx.Response(self.status, json={})

    def backend(self, **kwargs):
        return PineconeBackend("idx.svc.pinecone.io", "secret-key", EMBED, client=self.client, **kwargs)

    def body(self, i):
        return json.loads(self.requests[i].content)


def _match(doc_id, metadata=None, score=0.5):
    meta = {"wardowl_id": doc_id, "wardowl_text": doc_id, "wardowl_metadata": json.dumps(metadata or {})}
    return {"id": record_id(doc_id), "score": score, "metadata": meta}


def test_requests_carry_the_key_namespace_filter_and_batches():
    fake = Fake([{"matches": [_match("a", {"team": ["x"]})]}])
    backend = fake.backend(namespace="tenant-1")
    hits = backend.search("q", 3, In("team", ["x"]))
    assert [(h.id, h.metadata) for h in hits] == [("a", {"team": ["x"]})]
    first = fake.requests[0]
    assert str(first.url) == "https://idx.svc.pinecone.io/query"
    assert first.headers["Api-Key"] == "secret-key" and first.headers["X-Pinecone-Api-Version"] == "2026-07"
    assert fake.body(0)["namespace"] == "tenant-1" and fake.body(0)["topK"] == 3
    assert fake.body(0)["filter"] == {TERMS_FIELD: {"$in": [_h("team", "x")]}}
    backend.add([Document(f"d{i}", "t", {"team": "x"}) for i in range(250)] + [Document("d0", "again", {})])
    upserts = [fake.body(i) for i in range(1, 4)]
    assert sorted(len(u["vectors"]) for u in upserts) == [50, 100, 100]
    again = next(v for u in upserts for v in u["vectors"] if v["id"] == "d0")
    assert again["metadata"]["wardowl_text"] == "again"  # the last of a repeated id wins
    backend.set_metadata({"d1": {"team": ["y"]}})
    assert fake.body(4)["setMetadata"][TERMS_FIELD] == [_h("team", "y")]
    backend.delete(["d1", "missing"])
    assert fake.body(5)["ids"] == ["d1", "missing"]
    assert "secret-key" not in repr(backend)


def test_nothing_to_match_asks_nothing():
    fake = Fake()
    backend = fake.backend()
    assert backend.search("q", 0) == [] and backend.search("q", 3, In("team", [])) == []
    backend.add([])
    backend.set_metadata({})
    assert fake.requests == []


@pytest.mark.parametrize(
    ("reply", "status"),
    [({"matches": []}, 500), ({"nothing": 1}, 200), ({"matches": [{"id": "x", "score": 1}]}, 200)],
)
def test_failed_or_malformed_answers_raise(reply, status):
    with pytest.raises(RuntimeError, match="Pinecone"):
        Fake([reply], status).backend().search("q", 3)


def test_settings_are_checked_and_clear_tolerates_an_empty_namespace():
    fake = Fake(status=404)
    fake.backend().clear()
    for bad in ({"api_key": ""}, {"concurrency": 0}):
        with pytest.raises(ValueError):
            PineconeBackend("idx", **{"api_key": "k", "embed": EMBED, **bad})
    pytest.importorskip("httpx")
    PineconeBackend("http://localhost:5081", "k", EMBED).close()


def test_wait_polls_until_a_search_shows_the_writes():
    old, new = _match("a", {"team": "x"}), _match("a", {"team": "y"})
    fake = Fake([{"matches": [old]}, {"matches": [new]}, {"matches": [new]}, {"matches": []}])
    backend = fake.backend(wait=10.0)
    backend.set_metadata({"a": {"team": "y"}, "missing": {}})  # describe_index_stats, then two polls
    paths = [r.url.path for r in fake.requests]
    assert paths == ["/vectors/update", "/vectors/update", "/describe_index_stats", "/query", "/query"]
    backend.delete(["a"])  # one poll that still shows it, one that does not
    assert [r.url.path for r in fake.requests[5:]] == ["/vectors/delete", "/query", "/query"]
    with pytest.raises(TimeoutError):
        Fake([{"matches": []}] * 3).backend(wait=0.0).add([Document("b", "text", {})])


# ---- the Pinecone service ----------------------------------------------------------------------

needs_pinecone = pytest.mark.skipif(not KEY, reason="set WARDOWL_TEST_PINECONE_API_KEY to run against Pinecone")
_SPACES: list[str] = []


@pytest.fixture(scope="module")
def host():
    httpx = pytest.importorskip("httpx")
    headers = {"Api-Key": KEY, "X-Pinecone-Api-Version": "2026-07"}
    with httpx.Client(base_url="https://api.pinecone.io", headers=headers, timeout=30) as api:
        if api.get(f"/indexes/{INDEX}").status_code == 404:
            spec = {"_values": {"type": "dense_vector", "dimension": EMBED.dim, "metric": "cosine"}}
            deployment = {"deployment_type": "managed", "cloud": "aws", "region": "us-east-1"}
            body = {"name": INDEX, "deployment": deployment, "schema": {"fields": spec}}
            api.post("/indexes", json=body).raise_for_status()
        for _ in range(60):
            index = api.get(f"/indexes/{INDEX}").json()
            if index.get("status", {}).get("ready"):
                break
            time.sleep(3)
    yield index["host"]
    for space in _SPACES:
        PineconeBackend(index["host"], KEY, EMBED, namespace=space).clear()


def _factory(host, *, vectors=False):
    def make(docs):
        for space in _SPACES:  # a namespace per corpus, cleared when the next one starts
            PineconeBackend(host, KEY, EMBED, namespace=space).clear()
        _SPACES[:] = [f"ntk-{uuid.uuid4().hex[:12]}"]
        backend = PineconeBackend(host, KEY, None if vectors else EMBED, namespace=_SPACES[0], wait=60.0)
        backend.add(docs, [EMBED(d.text) for d in docs] if vectors else None)
        return backend

    return make


@needs_pinecone
def test_the_conformance_kit(host):
    check_backend(_factory(host), examples=8)


@needs_pinecone
def test_the_conformance_kit_with_query_vectors(host):
    check_backend(_factory(host, vectors=True), examples=4, query=Query(vector=EMBED(QUERY)))


@needs_pinecone
def test_pre_filtering_and_changes_on_pinecone(host):
    backend = _factory(host)(
        [
            Document("eng", "engineering launch plan", {"allowed_groups": ["eng"]}),
            Document("hr", "pay bands for the launch", {"allowed_groups": ["hr"]}),
            Document("all", "launch date for everyone", {"allowed_groups": ["*"]}),
            Document("ünïcode/π", "launch notes", {"allowed_groups": ["eng"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    found = {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=4).hits}
    assert found == {"eng", "all", "ünïcode/π"}
    backend.set_metadata({"hr": {"allowed_groups": ["eng"]}, "missing": {"allowed_groups": ["eng"]}})
    found = {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=5).hits}
    assert found == {"eng", "hr", "all", "ünïcode/π"}
    backend.delete(["ünïcode/π"])
    assert len(backend.search("launch", 10)) == 3
