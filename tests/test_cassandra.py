"""Cassandra: the OR of ANDs, settings, and the conformance kit against a real Cassandra."""

from __future__ import annotations

import os

import pytest

from needtoknow import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from needtoknow.backends._engines import tag
from needtoknow.backends.cassandra import CassandraBackend, create_table, filter_to_conjunctions
from needtoknow.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
HOST = os.environ.get("NEEDTOKNOW_TEST_CASSANDRA_HOST")


# ---- the OR of ANDs and settings (no server) ---------------------------------------------------


def test_filters_become_an_or_of_ands():
    a, b, c = tag("a", "x"), tag("b", "y"), tag("c", 1)
    assert filter_to_conjunctions(In("a", ["x"])) == [frozenset({a})]
    assert filter_to_conjunctions(And(In("a", ["x"]), Or(In("b", ["y"]), In("c", [1])))) == sorted(
        [frozenset({a, b}), frozenset({a, c})], key=sorted
    )
    assert filter_to_conjunctions(Or(In("a", ["x"]), In("a", ["x"]))) == [frozenset({a})]
    assert filter_to_conjunctions(In("a", [])) == []
    assert filter_to_conjunctions(And(In("a", ["x"]), In("b", []))) == []
    with pytest.raises(ValueError):
        filter_to_conjunctions(In("a\x1fb", ["x"]))
    with pytest.raises(TypeError):
        filter_to_conjunctions("a")
    wide = In("g", range(1000))
    with pytest.raises(ValueError, match="too many"):
        filter_to_conjunctions(And(wide, In("h", range(1000))))


class NoSession:
    def __getattr__(self, name):
        raise AssertionError(f"Cassandra was asked: {name}")


def test_settings_and_searches_that_need_no_query():
    backend = CassandraBackend(NoSession(), "ks", "chunks", EMBED, max_queries=2)
    assert backend.search("q", 0) == [] and backend.search("q", 3, In("team", [])) == []
    backend.add([])
    backend.delete([])
    with pytest.raises(ValueError, match="max_queries"):
        backend.search("q", 3, In("team", ["a", "b", "c"]))
    for bad in ({"keyspace": "Ks"}, {"table": "a;b"}):
        with pytest.raises(ValueError):
            CassandraBackend(NoSession(), **{"keyspace": "ks", "table": "t", **bad}, embed=EMBED)
    with pytest.raises(ValueError):
        CassandraBackend(NoSession(), "ks", "t", EMBED, similarity="manhattan")
    with pytest.raises(ValueError):
        CassandraBackend(NoSession(), "ks", "t", EMBED, concurrency=0)
    for args in (("ks", "t", 0), ("ks", "T", 4)):
        with pytest.raises(ValueError):
            create_table(NoSession(), *args)
    with pytest.raises(ValueError):
        create_table(NoSession(), "ks", "t", 4, similarity="manhattan")


# ---- a real Cassandra --------------------------------------------------------------------------

needs_cassandra = pytest.mark.skipif(not HOST, reason="set NEEDTOKNOW_TEST_CASSANDRA_HOST to run against Cassandra")
_STORED: set[str] = set()


@pytest.fixture(scope="module")
def session():
    cluster_module = pytest.importorskip("cassandra.cluster")
    cluster = cluster_module.Cluster([HOST])
    session = cluster.connect()
    session.execute(
        "CREATE KEYSPACE IF NOT EXISTS needtoknow_test "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}"
    )
    create_table(session, "needtoknow_test", "chunks", EMBED.dim)
    yield session
    cluster.shutdown()


def _factory(session, *, vectors=False):
    def make(docs):
        backend = CassandraBackend(session, "needtoknow_test", "chunks", None if vectors else EMBED)
        backend.delete(_STORED)  # one table for every test: each corpus replaces the one before
        _STORED.clear()
        backend.add(docs, [EMBED(d.text) for d in docs] if vectors else None)
        _STORED.update(d.id for d in docs)
        return backend

    return make


@needs_cassandra
def test_the_conformance_kit(session):
    check_backend(_factory(session))


@needs_cassandra
def test_the_conformance_kit_with_query_vectors(session):
    check_backend(_factory(session, vectors=True), examples=8, query=Query(vector=EMBED(QUERY)))


@needs_cassandra
def test_pre_filtering_and_changes_against_cassandra(session):
    backend = _factory(session)(
        [
            Document("eng", "engineering launch plan", {"allowed_groups": ["eng"]}),
            Document("hr", "pay bands for the launch", {"allowed_groups": ["hr"]}),
            Document("all", "launch date for everyone", {"allowed_groups": ["*"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    assert {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=3).hits} == {"eng", "all"}
    backend.set_metadata({"hr": {"allowed_groups": ["eng"]}, "missing": {"allowed_groups": ["eng"]}})
    assert {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=3).hits} == {"eng", "hr", "all"}
    assert backend.search("launch", 3, In("allowed_groups", ["eng"])) and len(backend.search("launch", 10)) == 3
