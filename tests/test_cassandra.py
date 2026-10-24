"""Cassandra: the OR of ANDs, settings, and the conformance kit against a real Cassandra."""

from __future__ import annotations

import os
import uuid

import pytest

from wardowl import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from wardowl.backends._engines import tag
from wardowl.backends.cassandra import (
    CassandraBackend,
    TooManyQueries,
    create_table,
    filter_to_conjunctions,
    required_terms,
)
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
HOST = os.environ.get("WARDOWL_TEST_CASSANDRA_HOST")


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
    with pytest.raises(ValueError, match="more than 10000"):
        filter_to_conjunctions(And(wide, In("h", range(1000))))
    with pytest.raises(ValueError, match="more than 5"):
        filter_to_conjunctions(wide, 5)
    assert filter_to_conjunctions(Or(In("a", ["x"]), And(In("a", ["x"]), In("b", ["y"])))) == [frozenset({a})]
    assert required_terms(And(In("a", ["x"]), Or(And(In("b", ["y"]), In("c", [1])), In("b", ["y"])))) == {a, b}
    assert required_terms(In("a", ["x", "z"])) == frozenset()
    with pytest.raises(TypeError):
        required_terms("a")


class NoSession:
    def __getattr__(self, name):
        raise AssertionError(f"Cassandra was asked: {name}")


def test_settings_and_searches_that_need_no_query():
    backend = CassandraBackend(NoSession(), "ks", "chunks", EMBED, max_queries=2)
    assert backend.search("q", 0) == [] and backend.search("q", 3, In("team", [])) == []
    backend.add([])
    backend.delete([])
    with pytest.raises(TooManyQueries):
        filter_to_conjunctions(In("team", ["a", "b", "c"]), 2)
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


def test_k_above_what_one_ann_query_returns_raises_instead_of_truncating():
    backend = CassandraBackend(NoSession(), "ks", "chunks", EMBED)
    assert backend.search("q", 1000, In("team", [])) == []
    with pytest.raises(ValueError, match="1,000"):
        backend.search("q", 1001)


def test_set_metadata_updates_the_rows_found_with_plain_writes(monkeypatch):
    calls = []

    class Found:
        def __init__(self, row):
            self.row = row

        def one(self):
            return self.row

    def run(self, cql, rows, *, write=False):
        calls.append((cql, list(rows), write))
        return [Found({"id": r[0]} if r[0] == "a" else None) for r in rows] if cql.startswith("SELECT") else []

    monkeypatch.setattr(CassandraBackend, "_run", run)
    CassandraBackend(NoSession(), "ks", "chunks", EMBED).set_metadata({"a": {"allowed_groups": ["hr"]}, "gone": {}})
    lookup, update = calls
    assert lookup == ("SELECT id FROM ks.chunks WHERE id = ?", [("a",), ("gone",)], False)
    # No lightweight transaction: its Paxos timestamp would not order with the plain writes of add and delete.
    assert update == (
        "UPDATE ks.chunks SET metadata = ?, wardowl_terms = ? WHERE id = ?",
        [('{"allowed_groups": ["hr"]}', {tag("allowed_groups", "hr")}, "a")],
        True,
    )


# ---- a real Cassandra --------------------------------------------------------------------------

needs_cassandra = pytest.mark.skipif(not HOST, reason="set WARDOWL_TEST_CASSANDRA_HOST to run against Cassandra")
_TABLES: list[str] = []


@pytest.fixture(scope="module")
def session():
    cluster_module = pytest.importorskip("cassandra.cluster")
    cluster = cluster_module.Cluster([HOST])
    session = cluster.connect()
    session.execute(
        "CREATE KEYSPACE IF NOT EXISTS wardowl_test "
        "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}"
    )
    yield session
    cluster.shutdown()


def _factory(session, *, vectors=False):
    def make(docs):
        # A new table for each corpus: rows deleted from the one before must not linger in its index.
        for old in _TABLES:
            session.execute(f"DROP TABLE IF EXISTS wardowl_test.{old}")
        _TABLES[:] = [f"chunks_{uuid.uuid4().hex[:12]}"]
        create_table(session, "wardowl_test", _TABLES[0], EMBED.dim)
        # The kit's deeply nested filters pass max_queries, so they also take the one-query path.
        backend = CassandraBackend(session, "wardowl_test", _TABLES[0], None if vectors else EMBED)
        backend.add(docs, [EMBED(d.text) for d in docs] if vectors else None)
        return backend

    return make


@needs_cassandra
def test_the_conformance_kit(session):
    # Cassandra's vector index (jvector) is approximate: a search can miss a matching row now and then,
    # even with k as large as the table. The kit still requires every hit to match the filter.
    check_backend(_factory(session), exact=False)


@needs_cassandra
def test_the_conformance_kit_with_query_vectors(session):
    check_backend(_factory(session, vectors=True), examples=8, query=Query(vector=EMBED(QUERY)), exact=False)


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
