"""FaissBackend: the conformance kit, exact pre-filtering, persistence and changes."""

from __future__ import annotations

import pytest

pytest.importorskip("faiss")

from wardowl import (
    Document,
    In,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    Subject,
)
from wardowl.backends.faiss import FaissBackend
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
DOCS = [
    Document("eng", "engineering launch plan", {"allowed_groups": ["eng"]}),
    Document("hr", "pay bands for the launch", {"allowed_groups": ["hr"]}),
    Document("all", "launch date for everyone", {"allowed_groups": ["*"]}),
]


def backend(docs=DOCS):
    b = FaissBackend(EMBED, EMBED.dim)
    b.add(docs)
    return b


def test_the_conformance_kit():
    check_backend(backend)


def test_the_conformance_kit_with_query_vectors():
    def make(docs):
        b = FaissBackend(None, EMBED.dim)
        b.add(docs, [EMBED(d.text) for d in docs])
        return b

    check_backend(make, examples=8, query=Query(vector=EMBED(QUERY)))


def test_pre_filtering_considers_only_permitted_chunks():
    retriever = PermissionedRetriever(backend(), MetadataACLAuthorizer(), strategy="prefilter")
    assert {h.id for h in retriever.retrieve("launch", Subject("ann", ["eng"]), k=3).hits} == {"eng", "all"}
    assert backend().search("launch", 3, In("allowed_groups", ["nobody"])) == []


def test_changes_replace_delete_and_set_metadata():
    b = backend()
    b.add([Document("hr", "pay bands, revised", {"allowed_groups": ["hr"]})])
    assert len(b) == 3 and b.index.ntotal == 3
    assert next(h for h in b.search("pay bands", 3) if h.id == "hr").text == "pay bands, revised"
    b.set_metadata({"hr": {"allowed_groups": ["eng"]}, "missing": {}})
    assert {h.id for h in b.search("launch", 3, In("allowed_groups", ["eng"]))} == {"eng", "hr"}
    b.delete(["eng", "missing"])
    assert len(b) == 2 and b.index.ntotal == 2 and "eng" not in {h.id for h in b.search("launch", 3)}
    b.add([])
    assert b.search("launch", 0) == [] and FaissBackend(EMBED, EMBED.dim).search("launch", 3) == []


def test_an_id_repeated_in_one_call_keeps_only_its_last_document():
    b = FaissBackend(EMBED, EMBED.dim)
    b.add(
        [
            Document("plan", "launch plan draft", {"allowed_groups": ["*"]}),
            Document("other", "launch notes", {"allowed_groups": ["eng"]}),
            Document("plan", "launch plan final", {"allowed_groups": ["hr"]}),
        ]
    )
    assert len(b) == 2 and b.index.ntotal == 2
    hits = b.search("launch plan", 5)
    assert sorted(h.id for h in hits) == ["other", "plan"]
    assert next(h for h in hits if h.id == "plan").text == "launch plan final"
    # The earlier copy, readable by everyone, must not be found under its old metadata.
    assert b.search("launch plan", 5, In("allowed_groups", ["*"])) == []
    b.set_metadata({"plan": {"allowed_groups": ["eng"]}})
    assert sorted(h.id for h in b.search("launch", 5, In("allowed_groups", ["eng"]))) == ["other", "plan"]
    b.delete(["plan"])
    assert [h.id for h in b.search("launch plan", 5)] == ["other"] and b.index.ntotal == 1


def test_repeated_ids_take_the_vector_of_their_last_document():
    b = FaissBackend(None, EMBED.dim)
    b.add(
        [Document("a", "first", {}), Document("a", "second", {})],
        [EMBED("salary bands"), EMBED("launch plan")],
    )
    (hit,) = b.search(Query(vector=EMBED("launch plan")), 5)
    assert hit.text == "second" and hit.score == pytest.approx(1.0, abs=1e-5)


def test_save_and_load(tmp_path):
    original = backend()
    original.save(tmp_path / "index")
    loaded = FaissBackend.load(tmp_path / "index", EMBED)
    assert [h.id for h in loaded.search("launch", 3)] == [h.id for h in original.search("launch", 3)]
    loaded.add([Document("new", "launch notes", {})])
    assert len(loaded) == 4
    (tmp_path / "index" / "chunks.json").write_text('{"dim": 16, "next": 0, "chunks": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="do not match"):
        FaissBackend.load(tmp_path / "index", EMBED)


def test_dimensions_are_checked():
    with pytest.raises(ValueError):
        FaissBackend(EMBED, 0)
    with pytest.raises(ValueError, match="dimensions"):
        FaissBackend(EMBED, 8, index=FaissBackend(EMBED, 16).index)
    with pytest.raises(ValueError, match="dimensions"):
        FaissBackend(EMBED, EMBED.dim).add([Document("x", "text")], [[1.0, 2.0]])
