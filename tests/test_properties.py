"""Property-based tests: the security guarantees must hold for every corpus, subject and query."""

from __future__ import annotations

import asyncio

from helpers import FilterIgnoringBackend, corpora, may_read, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import FunctionAuthorizer, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever

strategies = st.sampled_from(["auto", "prefilter", "postfilter"])
ks = st.integers(min_value=1, max_value=8)


@settings(max_examples=300)
@given(corpora(), subjects, queries, ks, strategies)
def test_never_returns_a_chunk_the_subject_may_not_read(docs, subject, query, k, strategy):
    retriever = PermissionedRetriever(InMemoryBackend(docs), MetadataACLAuthorizer(), strategy=strategy)
    result = retriever.retrieve(query, subject, k)
    assert all(may_read(subject, hit) for hit in result.hits)
    assert len(result.hits) <= k


@settings(max_examples=300)
@given(corpora(), subjects, queries, ks, strategies)
def test_returns_exactly_the_top_k_of_what_the_subject_may_read(docs, subject, query, k, strategy):
    # With max_candidates covering the corpus, every strategy must produce the correct answer.
    retriever = PermissionedRetriever(
        InMemoryBackend(docs), MetadataACLAuthorizer(), strategy=strategy, max_candidates=len(docs) + 1
    )
    result = retriever.retrieve(query, subject, k)
    assert [h.id for h in result.hits] == oracle_top_k(docs, subject, query, k)
    assert result.complete


@settings(max_examples=200)
@given(corpora(), subjects, queries, ks)
def test_a_backend_that_ignores_the_filter_cannot_leak(docs, subject, query, k):
    retriever = PermissionedRetriever(FilterIgnoringBackend(docs), MetadataACLAuthorizer(), strategy="prefilter")
    result = retriever.retrieve(query, subject, k)
    assert all(may_read(subject, hit) for hit in result.hits)
    leaked = [h for h in FilterIgnoringBackend(docs).search(query, k) if not may_read(subject, h)]
    assert result.violations == len(leaked)
    assert result.complete is (not leaked)


@settings(max_examples=200)
@given(corpora(), subjects, queries, ks)
def test_without_the_recheck_a_backend_that_ignores_the_filter_still_cannot_leak(docs, subject, query, k):
    acl, asked = MetadataACLAuthorizer(), []

    def check(subject, hits, action):
        asked.append(len(hits))
        return acl.check(subject, hits, action)

    authz = FunctionAuthorizer(check, acl.filter_for)
    retriever = PermissionedRetriever(FilterIgnoringBackend(docs), authz, strategy="prefilter", recheck=False)
    result = retriever.retrieve(query, subject, k)
    assert all(may_read(subject, hit) for hit in result.hits)
    leaked = [h for h in FilterIgnoringBackend(docs).search(query, k) if not may_read(subject, h)]
    assert result.violations == len(leaked) and result.complete is (not leaked)
    assert asked == [] and result.examined == 0  # the filter was checked locally, not by the authorizer


@settings(max_examples=100)
@given(corpora(), subjects, queries, ks)
def test_without_the_recheck_a_backend_that_filters_gives_exactly_the_reference_answer(docs, subject, query, k):
    backend, acl = InMemoryBackend(docs), MetadataACLAuthorizer()
    result = PermissionedRetriever(backend, acl, strategy="prefilter", recheck=False).retrieve(query, subject, k)
    assert [h.id for h in result.hits] == oracle_top_k(docs, subject, query, k)
    assert result.violations == 0 and result.complete


@settings(max_examples=100)
@given(corpora(), subjects, queries, ks, strategies)
def test_async_and_sync_agree(docs, subject, query, k, strategy):
    retriever = PermissionedRetriever(InMemoryBackend(docs), MetadataACLAuthorizer(), strategy=strategy)
    sync = retriever.retrieve(query, subject, k)
    async_ = asyncio.run(retriever.aretrieve(query, subject, k))
    assert async_ == sync
