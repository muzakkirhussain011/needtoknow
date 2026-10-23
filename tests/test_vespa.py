"""Vespa: YQL translation, the schema, replies, and the conformance kit against a real Vespa."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from needtoknow import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, RankFusion, Subject
from needtoknow.backends._engines import tag
from needtoknow.backends.vespa import TERMS_FIELD, VespaBackend, filter_to_yql, schema
from needtoknow.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
URL = os.environ.get("NEEDTOKNOW_TEST_VESPA_URL")
APP = Path(__file__).parent.parent / "examples" / "vespa" / "app"


def _h(field, value):
    return f'"{tag(field, value).encode().hex()}"'


# ---- translation and the schema (no server) ----------------------------------------------------


def test_filters_become_yql_on_hex_terms():
    assert filter_to_yql(In("team", ["b", 1])) == f"{TERMS_FIELD} in ({_h('team', 1)}, {_h('team', 'b')})"
    a, b, c = (f"{TERMS_FIELD} in ({_h(f, v)})" for f, v in [("a", "x"), ("b", "y"), ("c", "z")])
    assert filter_to_yql(And(In("a", ["x"]), Or(In("b", ["y"]), In("c", ["z"])))) == f"({a} and ({b} or {c}))"
    assert filter_to_yql(And(In("a", ["x"]))) == a


def test_hostile_values_become_hex_digits():
    yql = filter_to_yql(In("allowed_groups", ['x") or true or ("']))
    assert set(yql.split("(", 1)[1].strip(')"')) <= set("0123456789abcdef")


def test_an_empty_in_matches_nothing():
    assert filter_to_yql(In("team", [])) is None
    assert filter_to_yql(And(In("a", ["x"]), In("b", []))) is None
    assert filter_to_yql(Or(In("a", []), In("b", ["y"]))) == f"{TERMS_FIELD} in ({_h('b', 'y')})"
    assert filter_to_yql(Or(In("a", []))) is None
    with pytest.raises(ValueError):
        filter_to_yql(In("a\x1fb", ["x"]))
    with pytest.raises(TypeError):
        filter_to_yql("team")


def test_the_example_package_holds_the_schema():
    assert (APP / "schemas" / "needtoknow.sd").read_text(encoding="utf-8") == schema(16)
    with pytest.raises(ValueError):
        schema(0)
    with pytest.raises(ValueError):
        schema(8, "bad name")


# ---- requests and replies (a fake Vespa) -------------------------------------------------------


class Fake:
    def __init__(self, reply=None, status=200):
        httpx = pytest.importorskip("httpx")
        self.requests = []
        self.reply, self.status = reply, status
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        import httpx

        self.requests.append(request)
        if request.url.path == "/search/":
            return httpx.Response(self.status, json=self.reply)
        return httpx.Response(self.status, json={})

    def backend(self, **kwargs):
        return VespaBackend("http://vespa:8080/", EMBED, client=self.client, **kwargs)


def _hit(doc_id, metadata=None):
    fields = {"needtoknow_id": doc_id, "text": doc_id, "needtoknow_metadata": json.dumps(metadata or {})}
    return {"id": f"id:needtoknow:needtoknow::{doc_id}", "relevance": 0.5, "fields": fields}


def test_requests_carry_the_filter_vectors_and_escaped_ids():
    fake = Fake({"root": {"children": [_hit("a b", {"team": ["x"]})]}})
    backend = fake.backend(approximate=False)
    hits = backend.search("q", 3, In("team", ["x"]))
    assert [(h.id, h.metadata) for h in hits] == [("a b", {"team": ["x"]})]
    body = json.loads(fake.requests[0].content)
    assert body["yql"] == (
        "select * from needtoknow where {targetHits: 3, approximate: false}nearestNeighbor(embedding, q)"
        f" and {TERMS_FIELD} in ({_h('team', 'x')})"
    )
    assert body["hits"] == 3 and len(body["input.query(q)"]) == EMBED.dim
    backend.add([Document("a/b", "one", {"team": "x"}), Document("a/b", "two", {})])
    backend.set_metadata({"a/b": {"team": ["y"]}})
    backend.delete(["a/b", "c"])
    put, update = json.loads(fake.requests[1].content), json.loads(fake.requests[2].content)
    assert fake.requests[1].url.raw_path == b"/document/v1/needtoknow/needtoknow/docid/a%2Fb"
    assert put["fields"]["text"] == "two" and len(fake.requests) == 5  # the last of a repeated id wins
    assert update["fields"][TERMS_FIELD]["assign"] == [tag("team", "y").encode().hex()]


def test_keyword_mode_sends_the_text():
    fake = Fake({"root": {}})
    backend = VespaBackend("http://vespa:8080", None, mode="keyword", client=fake.client)
    assert backend.search("leave policy", 5) == []
    body = json.loads(fake.requests[0].content)
    assert body["query"] == "leave policy" and body["ranking.profile"] == "needtoknow_bm25"
    backend.add([Document("a", "text", {})])
    assert "embedding" not in json.loads(fake.requests[1].content)["fields"]
    with pytest.raises(ValueError, match="text"):
        backend.search(Query(vector=EMBED("x")), 5)


def test_nothing_to_match_asks_nothing():
    fake = Fake()
    backend = fake.backend()
    assert backend.search("q", 0) == [] and backend.search("q", 3, In("team", [])) == []
    backend.add([])
    backend.delete([])
    assert fake.requests == []


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        ({"root": {"errors": [{"code": 12, "message": "Timeout"}], "children": []}}, 200),
        ({"root": {}}, 500),
        ({"nothing": 1}, 200),
        ({"root": {"children": [{"id": "x", "relevance": 1.0, "fields": {}}]}}, 200),
    ],
)
def test_failed_or_malformed_searches_raise(reply, status):
    with pytest.raises(RuntimeError, match="Vespa"):
        Fake(reply, status).backend().search("q", 3)


def test_failed_writes_raise_and_settings_are_checked():
    backend = Fake(status=507).backend()
    with pytest.raises(RuntimeError, match="write failed"):
        backend.add([Document("a", "x", {}), Document("b", "y", {})])
    for bad in ({"schema": "a.b"}, {"namespace": ""}, {"mode": "hybrid"}, {"concurrency": 0}):
        with pytest.raises(ValueError):
            Fake().backend(**bad)
    pytest.importorskip("httpx")
    own = VespaBackend("http://vespa:8080", EMBED)
    own.close()


# ---- a real Vespa ------------------------------------------------------------------------------

needs_vespa = pytest.mark.skipif(not URL, reason="set NEEDTOKNOW_TEST_VESPA_URL to run against Vespa")
_STORED: set[str] = set()


def _factory(*, mode="vector", approximate=False, vectors=False, hybrid=False):
    httpx = pytest.importorskip("httpx")
    client = httpx.Client(timeout=30)
    stored = _STORED  # one Vespa for every test: each corpus replaces the one before

    def make(docs):
        embed = None if vectors or mode == "keyword" else EMBED
        backend = VespaBackend(URL, embed, mode=mode, approximate=approximate, client=client)
        backend.delete(stored)
        stored.clear()
        backend.add(docs, [EMBED(d.text) for d in docs] if vectors else None)
        stored.update(d.id for d in docs)
        if hybrid:
            return RankFusion(backend, VespaBackend(URL, None, mode="keyword", client=client))
        return backend

    return make


@needs_vespa
def test_the_conformance_kit_exact():
    check_backend(_factory())


@needs_vespa
def test_the_conformance_kit_with_hnsw():
    check_backend(_factory(approximate=True), examples=10, exact=False)


@needs_vespa
def test_the_conformance_kit_with_query_vectors():
    check_backend(_factory(vectors=True), examples=8, query=Query(vector=EMBED(QUERY)))


@needs_vespa
def test_keyword_mode_passes_the_conformance_kit():
    check_backend(_factory(mode="keyword"), examples=10)


@needs_vespa
def test_hybrid_search_passes_the_conformance_kit():
    check_backend(_factory(hybrid=True), examples=8)


@needs_vespa
def test_pre_filtering_against_vespa():
    backend = _factory()(
        [
            Document("eng", "engineering launch plan", {"allowed_groups": ["eng"]}),
            Document("hr", "pay bands for the launch", {"allowed_groups": ["hr"]}),
            Document("all", "launch date for everyone", {"allowed_groups": ["*"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    assert {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=3).hits} == {"eng", "all"}
