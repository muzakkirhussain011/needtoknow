"""RankFusion: hybrid search over several backends, fused by reciprocal rank fusion."""

from __future__ import annotations

import asyncio
import logging

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import (
    Document,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    RankFusion,
    Subject,
)
from needtoknow.testing import check_backend


class Fixed:
    """A backend that returns a fixed ranking and records what it was asked."""

    supports_filters = True

    def __init__(self, *hits):
        self.hits, self.calls = list(hits), []

    def search(self, query, k, filter=None):
        self.calls.append((query, k, filter))
        return self.hits[:k]


def _hit(doc_id, metadata=None):
    return Hit(doc_id, f"text {doc_id}", 1.0, metadata or {"allowed_groups": ["*"]})


def test_fusing_a_store_with_itself_passes_the_conformance_kit():
    check_backend(lambda docs: RankFusion(InMemoryBackend(docs), InMemoryBackend(docs)))


@settings(max_examples=100, deadline=None)
@given(corpora(max_size=20), subjects, queries, st.integers(1, 5), st.sampled_from(["auto", "prefilter", "postfilter"]))
def test_retrieval_through_fusion_matches_the_oracle(docs, subject, query, k, strategy):
    fused = RankFusion(InMemoryBackend(docs), InMemoryBackend(docs))
    retriever = PermissionedRetriever(fused, MetadataACLAuthorizer(), strategy=strategy, max_candidates=len(docs) + 1)
    assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


def test_ranks_are_fused_with_weights():
    vector, keyword = Fixed(_hit("a"), _hit("b"), _hit("c")), Fixed(_hit("b"), _hit("c"), _hit("a"))
    assert [h.id for h in RankFusion(vector, keyword).search("q", 3)] == ["b", "a", "c"]
    assert [h.id for h in RankFusion(vector, keyword, weights=[3, 1]).search("q", 3)] == ["a", "b", "c"]
    (top,) = RankFusion(vector, keyword, rank_constant=0).search("q", 1)
    assert top.id == "b" and top.score == pytest.approx(1 / 2 + 1 / 1)
    assert top.text == "text b" and top.metadata == {"allowed_groups": ["*"]}


def test_every_backend_gets_the_filter_and_the_depth():
    one, two = Fixed(_hit("a")), Fixed(_hit("b"))
    f = In("allowed_groups", ["hr"])
    RankFusion(one, two, depth=20).search("leave", 3, f)
    RankFusion(one, two).search("leave", 4)
    assert one.calls == two.calls == [("leave", 20, f), ("leave", 4, None)]


def test_a_chunk_with_different_metadata_in_two_backends_is_left_out(caplog):
    current, stale = Fixed(_hit("a", {"allowed_groups": ["hr"]}), _hit("b")), Fixed(_hit("a"), _hit("b"))
    with caplog.at_level(logging.WARNING, logger="needtoknow"):
        hits = RankFusion(current, stale).search("q", 5)
    assert [h.id for h in hits] == ["b"]
    assert "1 chunks whose metadata differs" in caplog.text
    # The same rule within one backend that returns a chunk twice.
    repeated = Fixed(_hit("a"), _hit("a", {"allowed_groups": ["hr"]}), _hit("b"))
    assert [h.id for h in RankFusion(repeated).search("q", 5)] == ["b"]
    assert [h.id for h in RankFusion(Fixed(_hit("a"), _hit("a"))).search("q", 5)] == ["a"]


def test_filters_are_supported_only_when_every_backend_supports_them():
    class NoFilters(Fixed):
        supports_filters = False

    assert RankFusion(Fixed(), Fixed()).supports_filters is True
    assert RankFusion(Fixed(), NoFilters()).supports_filters is False
    docs = [Document("a", "leave", {"allowed_groups": ["hr"]}), Document("b", "leave", {"allowed_groups": ["eng"]})]
    retriever = PermissionedRetriever(RankFusion(InMemoryBackend(docs), NoFilters()), MetadataACLAuthorizer())
    result = retriever.retrieve("leave", Subject("someone", ["eng"]), 5)
    assert result.strategy == "postfilter" and [h.id for h in result.hits] == ["b"]


def test_wrong_configuration_is_rejected():
    for args, options in [
        ((), {}),
        ((Fixed(),), {"weights": [1, 2]}),
        ((Fixed(),), {"weights": [0]}),
        ((Fixed(),), {"weights": [float("nan")]}),
        ((Fixed(),), {"rank_constant": -1}),
        ((Fixed(),), {"depth": 0}),
    ]:
        with pytest.raises(ValueError):
            RankFusion(*args, **options)
    assert RankFusion(Fixed(_hit("a"))).search("q", 0) == []


def test_asearch_searches_the_backends_concurrently_with_the_same_results():
    class Native(Fixed):
        async def asearch(self, query, k, filter=None):
            self.calls.append(("async", k, filter))
            return self.hits[:k]

    native, threaded = Native(_hit("a"), _hit("b")), Fixed(_hit("b"), _hit("a"))
    fused = RankFusion(native, threaded)
    assert asyncio.run(fused.asearch("q", 2)) == fused.search("q", 2)
    assert native.calls[0] == ("async", 2, None)
    assert asyncio.run(fused.asearch("q", 0)) == []
