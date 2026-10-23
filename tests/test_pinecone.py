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
    # Never None: Pinecone reads a missing filter as "no filter", which matches every record.
    for nothing in (In("a", []), And(In("a", ["x"]), In("b", [])), Or(In("a", []))):
        with pytest.raises(NothingMatches):
            filter_to_pinecone(nothing)
    assert filter_to_pinecone(Or(In("a", []), In("b", ["y"]))) == b
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


def test_record_ids_are_never_shared_by_two_chunk_ids():
    # A chunk named after another chunk's hashed record id is hashed too, so it gets a record of its own.
    hashed = record_id("café")
    assert record_id(hashed) != hashed and record_id(hashed).startswith("sha256-")
    assert record_id("sha256-") != "sha256-" and record_id("SHA256-x") == "SHA256-x"


# ---- requests and replies (a fake Pinecone) ----------------------------------------------------


class Fake:
    def __init__(self, replies=None, status=200, records=None):
        httpx = pytest.importorskip("httpx")
        self.requests, self.replies, self.status = [], list(replies or []), status
        self.records = dict(records or {})  # record id -> metadata, for fetches
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        import httpx

        self.requests.append(request)
        if request.url.path == "/describe_index_stats":
            return httpx.Response(200, json={"dimension": EMBED.dim})
        if request.url.path == "/query":
            return httpx.Response(self.status, json=self.replies.pop(0) if self.replies else {"matches": []})
        if request.url.path == "/vectors/fetch":
            asked = request.url.params.get_list("ids")
            found = {r: {"id": r, "metadata": self.records[r]} for r in asked if r in self.records}
            return httpx.Response(self.status, json={"vectors": found, "namespace": ""})
        return httpx.Response(self.status, json={})

    def backend(self, **kwargs):
        kwargs.setdefault("wait", None)  # the fake's searches never show the writes
        return PineconeBackend("idx.svc.pinecone.io", "secret-key", EMBED, client=self.client, **kwargs)

    def paths(self):
        return [r.url.path for r in self.requests]

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests if r.url.path == path]

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
    (update,) = fake.bodies("/vectors/update")
    assert update["setMetadata"][TERMS_FIELD] == [_h("team", "y")]
    backend.delete(["d1", "missing"])
    assert fake.bodies("/vectors/delete") == [{"ids": ["d1", "missing"], "namespace": "tenant-1"}]
    fetches = [r for r in fake.requests if r.url.path == "/vectors/fetch"]
    assert [r.method for r in fetches] == ["GET", "GET"] and fetches[1].url.params.get_list("ids") == ["d1", "missing"]
    assert all(r.url.params["namespace"] == "tenant-1" and r.headers["Api-Key"] == "secret-key" for r in fetches)
    assert "secret-key" not in repr(backend)


def test_nothing_to_match_asks_nothing():
    fake = Fake()
    backend = fake.backend()
    assert backend.search("q", 0) == [] and backend.search("q", 3, In("team", [])) == []
    backend.add([])
    backend.set_metadata({})
    backend.delete([])
    with pytest.raises(ValueError, match="10,000"):
        backend.search("q", 10_001)  # Pinecone would answer with 10,000, as if no more matched
    assert fake.requests == []


def test_changes_leave_alone_a_record_that_holds_another_id():
    # A record at victim's record id holding another chunk id: written there by something else.
    fake = Fake(records={record_id("victim"): {"wardowl_id": "intruder"}, record_id("mine"): {"wardowl_id": "mine"}})
    backend = fake.backend()
    backend.set_metadata({"victim": {"allowed_groups": ["*"]}, "mine": {"allowed_groups": ["*"]}, "unseen": {}})
    assert sorted(u["id"] for u in fake.bodies("/vectors/update")) == ["mine", "unseen"]
    backend.delete(["victim", "mine", "unseen"])
    assert [d["ids"] for d in fake.bodies("/vectors/delete")] == [["mine", "unseen"]]


def test_fetches_keep_their_urls_short():
    fake = Fake()
    ids = [f"{i:03d}/" + "x" * 500 for i in range(30)]  # long ids, each "/" encoded as %2F
    fake.backend().delete(ids)
    fetches = [r for r in fake.requests if r.url.path == "/vectors/fetch"]
    assert len(fetches) > 1 and all(len(str(r.url)) < 8_000 for r in fetches)
    assert [i for r in fetches for i in r.url.params.get_list("ids")] == ids


def test_writes_wait_for_searches_to_see_them_by_default():
    pytest.importorskip("httpx")
    assert PineconeBackend("idx", "k", EMBED, client=Fake().client).wait == 30.0


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
    writes = ["/vectors/fetch", "/vectors/update", "/vectors/update"]
    assert fake.paths() == [*writes, "/describe_index_stats", "/query", "/query"]
    backend.delete(["a"])  # one poll that still shows it, one that does not
    assert fake.paths()[6:] == ["/vectors/fetch", "/vectors/delete", "/query", "/query"]
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
