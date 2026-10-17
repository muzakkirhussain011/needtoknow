"""LanceDBBackend on an embedded LanceDB database in a temporary directory."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

pytest.importorskip("lancedb")

import lancedb

from needtoknow import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from needtoknow.backends._engines import TERMS_FIELD, tag
from needtoknow.backends.lancedb import (
    KEY_FIELD,
    LanceDBBackend,
    create_indexes,
    create_table,
    filter_to_sql,
    permission_terms,
)
from needtoknow.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()


@pytest.fixture
def db(tmp_path):
    return lancedb.connect(str(tmp_path / "lance"))


def _new_table(db):
    return create_table(db, f"ntk_{uuid.uuid4().hex}", EMBED.dim)


# ---- translation -------------------------------------------------------------------------------


def test_filters_become_array_has_any_on_hex_terms():
    hr, one = tag("team", "hr").encode().hex(), tag("level", 1).encode().hex()
    assert filter_to_sql(In("team", ["hr"])) == f"array_has_any({TERMS_FIELD}, ['{hr}'])"
    both = filter_to_sql(And(In("team", ["hr"]), In("level", [1])))
    assert both == f"(array_has_any({TERMS_FIELD}, ['{hr}'])) AND (array_has_any({TERMS_FIELD}, ['{one}']))"
    assert " OR " in filter_to_sql(Or(In("team", ["hr"]), In("level", [1])))


def test_an_empty_in_matches_nothing_without_asking_lancedb():
    assert filter_to_sql(In("team", [])) is None
    assert filter_to_sql(And(In("team", ["a"]), In("level", []))) is None
    assert filter_to_sql(Or(In("team", []), In("level", [1]))) == filter_to_sql(In("level", [1]))


def test_values_never_reach_the_sql_unencoded():
    hostile = "x') OR TRUE --"
    sql = filter_to_sql(In("allowed_groups", [hostile, "O'Brien"]))
    assert "OR TRUE" not in sql and "O'Brien" not in sql
    with pytest.raises(ValueError):
        filter_to_sql(In("team\x1f", ["a"]))


# ---- an embedded database ----------------------------------------------------------------------


def test_passes_the_conformance_kit(db):
    def make(docs):
        backend = LanceDBBackend(_new_table(db), EMBED)
        backend.add(docs)
        return backend

    check_backend(make)


def test_searches_with_query_vectors_and_no_embed_function(db):
    def make(docs):
        backend = LanceDBBackend(_new_table(db))
        backend.add(docs, [EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(QUERY)))


def test_the_label_list_index_gives_the_same_answers(db):
    table = _new_table(db)
    backend = LanceDBBackend(table, EMBED)
    docs = [Document(f"d{i}", f"doc leave {i}", {"allowed_groups": [f"g{i % 7}"], "level": i % 3}) for i in range(60)]
    backend.add(docs)
    f = Or(In("allowed_groups", ["g1", "g3"]), And(In("level", [2]), In("allowed_groups", ["g5"])))
    before = [h.id for h in backend.search("leave", 60, f)]
    create_indexes(table)
    assert [h.id for h in backend.search("leave", 60, f)] == before and before


def test_hostile_and_quoted_values_are_only_values(db):
    backend = LanceDBBackend(_new_table(db), EMBED)
    backend.add(
        [
            Document("o'brien's notes", "leave notes", {"allowed_users": ["O'Brien"]}),
            Document("secret", "leave secret", {"allowed_groups": ["board"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    attacker = Subject("mallory", ["x') OR TRUE --", "' OR 1=1 --"])
    assert retriever.retrieve("leave", attacker, 5).hits == []
    assert [h.id for h in retriever.retrieve("leave", Subject("O'Brien"), 5).hits] == ["o'brien's notes"]
    backend.delete(["o'brien's notes"])
    assert [h.id for h in backend.search("leave", 5)] == ["secret"]


def test_a_permission_change_applies_at_once_and_duplicates_keep_the_last(db):
    backend = LanceDBBackend(_new_table(db), EMBED)
    bob = Subject("bob", ["eng"])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    backend.add([Document("plan", "leave plan", {"allowed_groups": ["eng"]})])
    assert [h.id for h in retriever.retrieve("leave", bob, 5).hits] == ["plan"]
    backend.add(
        [
            Document("plan", "leave plan", {"allowed_groups": ["eng"]}),
            Document("plan", "leave plan", {"allowed_groups": ["hr"]}),
        ]
    )
    assert retriever.retrieve("leave", bob, 5).hits == []
    assert len(backend.search("leave", 5)) == 1


def test_rows_written_by_another_tool_need_their_terms(db):
    table = _new_table(db)
    backend = LanceDBBackend(table, EMBED)
    metadata = {"allowed_groups": ["hr"]}
    row = {
        "id": "raw",
        KEY_FIELD: b"raw".hex(),
        "content": "leave policy",
        "metadata": json.dumps(metadata),
        "vector": EMBED("leave policy"),
    }
    table.add([row | {TERMS_FIELD: []}])
    ann = Subject("ann", ["hr"])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    assert retriever.retrieve("leave", ann, 5).hits == []
    table.delete(f"{KEY_FIELD} = '{b'raw'.hex()}'")
    table.add([row | {TERMS_FIELD: permission_terms(metadata)}])
    assert [h.id for h in retriever.retrieve("leave", ann, 5).hits] == ["raw"]


def test_rows_without_readable_metadata_are_denied(db):
    table = _new_table(db)
    backend = LanceDBBackend(table, EMBED)
    row = {"id": "bad", KEY_FIELD: b"bad".hex(), "content": "leave", "metadata": "not json"}
    table.add([row | {TERMS_FIELD: [], "vector": EMBED("leave")}])
    (hit,) = backend.search("leave", 5)
    assert hit.metadata == {}
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="postfilter").retrieve(
        "leave", Subject("bob", ["eng"]), 5
    )
    assert result.hits == [] and result.denied == 1


def test_async_retrieval_matches_sync(db):
    backend = LanceDBBackend(_new_table(db), EMBED)
    backend.add([Document(f"d{i}", f"leave {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    bob = Subject("bob", ["eng"])
    assert asyncio.run(retriever.aretrieve("leave", bob, 4)) == retriever.retrieve("leave", bob, 4)


def test_arguments_are_checked(db):
    with pytest.raises(ValueError):
        create_table(db, "bad", 0)
    backend = LanceDBBackend(_new_table(db), EMBED)
    with pytest.raises(ValueError, match="one embedding per document"):
        backend.add([Document("a", "t")], embeddings=[])
    assert backend.search("anything", 0) == []
    assert backend.search("anything", 3, In("team", [])) == []


def test_set_metadata_replaces_permissions_and_ignores_unknown_ids(db):
    backend = LanceDBBackend(_new_table(db), EMBED)
    backend.add([Document("plan", "leave plan", {"allowed_groups": ["eng"], "level": 1})])
    backend.set_metadata({"plan": {"allowed_groups": ["O'Brien's team"]}, "not-stored": {"allowed_groups": ["*"]}})
    assert backend.search("leave", 5, In("allowed_groups", ["eng"])) == []
    assert backend.search("leave", 5, In("level", [1])) == []
    (hit,) = backend.search("leave", 5)
    assert hit.id == "plan" and hit.text == "leave plan" and hit.metadata == {"allowed_groups": ["O'Brien's team"]}


# ---- keyword search and hybrid search ----------------------------------------------------------


def _indexed_for_keywords(backend):
    # The full-text index is built on a seed row, then the corpus arrives after it, as in production:
    # rows added after the index are found by a scan until the table is optimized.
    backend.add([Document("seed", "seed", {})])
    create_indexes(backend.table, content_field="content")
    backend.delete(["seed"])
    return backend


def test_mode_is_checked():
    with pytest.raises(ValueError):
        LanceDBBackend(None, mode="semantic")


def test_keyword_mode_passes_the_conformance_kit(db):
    def make(docs):
        backend = _indexed_for_keywords(LanceDBBackend(_new_table(db), mode="keyword"))
        backend.add(docs)
        return backend

    check_backend(make)


def test_hybrid_search_over_one_table_passes_the_conformance_kit(db):
    from needtoknow import RankFusion

    def make(docs):
        vector = _indexed_for_keywords(LanceDBBackend(_new_table(db), EMBED))
        vector.add(docs)
        return RankFusion(vector, LanceDBBackend(vector.table, mode="keyword"))

    check_backend(make, examples=10)
    check_backend(make, examples=5, query=Query(QUERY, EMBED(QUERY)))


def test_keyword_mode_ranks_by_bm25_inside_the_filter(db):
    table = _new_table(db)
    keyword = LanceDBBackend(table, mode="keyword")
    keyword.add(
        [
            Document("a", "leave policy for staff", {"allowed_groups": ["*"]}),
            Document("b", "leave leave leave payouts", {"allowed_groups": ["hr"]}),
            Document("c", "the incident runbook", {"allowed_groups": ["*"]}),
        ]
    )
    create_indexes(table, content_field="content")
    hits = keyword.search("leave", 5)
    assert [h.id for h in hits] == ["b", "a"] and hits[0].score > hits[1].score > 0
    assert [h.id for h in keyword.search("leave", 5, In("allowed_groups", ["*"]))] == ["a"]
    assert [h.id for h in keyword.search("the", 5)] == ["c"]  # no stop word is dropped
    hostile = "leave\" OR content:* ( ) ' --"
    assert keyword.search(hostile, 5, In("allowed_groups", ["nobody"])) == []
    assert {h.id for h in keyword.search(hostile, 5, In("allowed_groups", ["*"]))} == {"a"}
    assert keyword.search("?! --", 5) == []
    with pytest.raises(ValueError):
        keyword.search(Query(vector=EMBED("leave")), 5)
    keyword.add([Document("d", "leave later", {"allowed_groups": ["*"]})])
    assert "d" in {h.id for h in keyword.search("leave", 5)}  # found before the index has it
    table.optimize()
    assert "d" in {h.id for h in keyword.search("leave", 5)}
    keyword.set_metadata({"d": {"allowed_groups": ["hr"]}})
    assert [h.id for h in keyword.search("later", 5, In("allowed_groups", ["hr"]))] == ["d"]
