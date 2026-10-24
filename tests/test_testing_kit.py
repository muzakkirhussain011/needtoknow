"""The conformance kit must pass correct implementations and catch each kind of broken one."""

from __future__ import annotations

import dataclasses
import json
import unicodedata

import pytest
from helpers import FilterIgnoringBackend

from wardowl import (
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    Subject,
    filter_to_dict,
    matches,
)
from wardowl.testing import ConformanceError, HashingEmbedder, check_authorizer, check_backend


def test_the_reference_backend_conforms():
    check_backend(InMemoryBackend, examples=60)


def test_a_backend_that_ignores_filters_is_caught():
    with pytest.raises(ConformanceError, match="should not match"):
        check_backend(FilterIgnoringBackend)


class _MetadataLosingBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return [dataclasses.replace(h, metadata={}) for h in super().search(query, k, filter)]


def test_a_backend_that_changes_metadata_is_caught():
    with pytest.raises(ConformanceError, match="metadata changed"):
        check_backend(_MetadataLosingBackend)


class _UnboundedBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return super().search(query, k + 5, filter)


def test_a_backend_that_ignores_k_is_caught():
    with pytest.raises(ConformanceError, match="hits for k="):
        check_backend(_UnboundedBackend)


class _UnorderedBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return list(reversed(super().search(f"{query} policy", k, filter)))


def test_a_backend_that_returns_hits_out_of_order_is_caught():
    with pytest.raises(ConformanceError, match="not ordered best first"):
        check_backend(_UnorderedBackend)


class _ShortBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return super().search(query, k, filter)[:-1]


def test_a_backend_that_misses_matches_is_caught_unless_approximate():
    with pytest.raises(ConformanceError, match="missed documents"):
        check_backend(_ShortBackend)
    # An approximate index may miss matches, as long as it never returns a wrong one.
    check_backend(_ShortBackend, exact=False)


class _BadAsyncBackend(InMemoryBackend):
    async def asearch(self, query, k, filter=None):
        return list(reversed(self.search(query, k, filter)))


def test_asearch_must_agree_with_search():
    with pytest.raises(ConformanceError, match="asearch and search disagree"):
        check_backend(_BadAsyncBackend)


class _AsyncScoresBackend(InMemoryBackend):
    async def asearch(self, query, k, filter=None):
        return [dataclasses.replace(h, score=h.score / 2) for h in self.search(query, k, filter)]


class _AsyncMetadataLosingBackend(InMemoryBackend):
    async def asearch(self, query, k, filter=None):
        return [dataclasses.replace(h, metadata={}) for h in self.search(query, k, filter)]


class _AsyncUnfilteredBackend(InMemoryBackend):
    """Its async path is wrong only for searches without a filter."""

    async def asearch(self, query, k, filter=None):
        hits = self.search(query, k, filter)
        return hits if filter is not None else list(reversed(hits))


@pytest.mark.parametrize(
    ("backend", "problem"),
    [(_AsyncScoresBackend, "score"), (_AsyncMetadataLosingBackend, "metadata"), (_AsyncUnfilteredBackend, "")],
)
def test_asearch_must_return_the_same_hits_as_search_with_or_without_a_filter(backend, problem):
    # Only the ids of filtered searches were compared; the scores, metadata and unfiltered searches were not.
    with pytest.raises(ConformanceError, match="asearch and search disagree") as failure:
        check_backend(backend)
    assert problem in str(failure.value)


class _AsyncEveryoneBug(InMemoryBackend):
    """Its async path drops a filter naming "*", the everyone marker: only retrieval asks for one."""

    async def asearch(self, query, k, filter=None):
        if filter is not None and '"*"' in json.dumps(filter_to_dict(filter)):
            filter = None
        return self.search(query, k, filter)


def test_retrieval_is_checked_through_aretrieve_too():
    with pytest.raises(ConformanceError, match="aretrieve with strategy='prefilter'"):
        check_backend(_AsyncEveryoneBug)


def _lenient(normalise):
    """A backend that compares values after `normalise`, as a store that folds case, normalises
    Unicode or compares numbers as text would: it matches values the reference keeps apart."""

    def each(value):
        return [normalise(v) for v in value] if isinstance(value, list) else normalise(value)

    def mapped(f):
        if isinstance(f, In):
            return In(f.field, [normalise(v) for v in f.values])
        return type(f)(*(mapped(c) for c in f.clauses))

    class Lenient(InMemoryBackend):
        def search(self, query, k, filter=None):
            hits = super().search(query, len(self) + 1)
            if filter is not None:
                hits = [h for h in hits if matches(mapped(filter), {n: each(v) for n, v in h.metadata.items()})]
            return hits[:k]

    return Lenient


@pytest.mark.parametrize(
    "normalise",
    [
        lambda v: v.lower() if isinstance(v, str) else v,  # "A" is "a"
        lambda v: unicodedata.normalize("NFC", v) if isinstance(v, str) else v,  # "é" is "é"
        lambda v: str(v) if type(v) is int else v,  # 1 is "1"
    ],
    ids=["case", "unicode", "type"],
)
def test_a_backend_that_matches_values_the_reference_keeps_apart_is_caught(normalise):
    # The kit's data once held none of these pairs, so such a backend passed it.
    with pytest.raises(ConformanceError):
        check_backend(_lenient(normalise), examples=100)


class _DottedPathBackend(InMemoryBackend):
    """Reads a dotted field name as a path into nested objects, as some query languages do."""

    def search(self, query, k, filter=None):
        hits = super().search(query, len(self) + 1)
        if filter is not None:
            hits = [h for h in hits if matches(filter, _paths(h.metadata))]
        return hits[:k]


def _paths(metadata):
    found = dict(metadata)
    for key, value in metadata.items():
        if isinstance(value, dict):
            found.update({f"{key}.{inner}": v for inner, v in value.items()})
    return found


def test_a_backend_that_reads_dots_as_paths_is_caught():
    with pytest.raises(ConformanceError):
        check_backend(_DottedPathBackend, examples=300)


class _RefusingBackend(InMemoryBackend):
    """Refuses filters on field names with a dot, as the Qdrant backend does: safe, so allowed."""

    def search(self, query, k, filter=None):
        if filter is not None and "." in json.dumps(filter_to_dict(filter)):
            raise ValueError("a dot is path syntax here")
        return super().search(query, k, filter)


def test_a_backend_may_refuse_a_field_name_it_cannot_filter_on():
    check_backend(_RefusingBackend)


def test_failures_say_how_to_reproduce_them():
    with pytest.raises(ConformanceError) as failure:
        check_backend(FilterIgnoringBackend, seed=7)
    message = str(failure.value)
    assert "seed=7" in message and "documents (" in message and "filter:" in message


def test_the_built_in_authorizers_conform():
    check_authorizer(MetadataACLAuthorizer())
    check_authorizer(CachingAuthorizer(MetadataACLAuthorizer(), ttl=60))


def test_an_authorizer_whose_filter_and_check_disagree_is_caught():
    acl = MetadataACLAuthorizer()
    # check() forgets the "everyone" marker that filter_for() includes.
    stricter = FunctionAuthorizer(
        check=lambda subject, hits, action: [
            subject.id in (h.metadata.get("allowed_users") or [])
            or bool(set(h.metadata.get("allowed_groups") or []) & subject.groups)
            for h in hits
        ],
        filter_for=acl.filter_for,
    )
    with pytest.raises(ConformanceError, match="filter_for and check disagree"):
        check_authorizer(stricter)


def test_an_authorizer_whose_answers_depend_on_the_batch_is_caught():
    # Allows only the first hit of every batch: the answer for a hit depends on its neighbours.
    first_only = FunctionAuthorizer(check=lambda subject, hits, action: [i == 0 for i in range(len(hits))])
    with pytest.raises(ConformanceError, match="depended on the other hits"):
        check_authorizer(first_only)


def test_a_cache_cannot_hide_an_authorizer_whose_answers_depend_on_the_batch():
    # The answers for each hit alone and in reverse order once came from the cache the first call filled.
    first_only = FunctionAuthorizer(check=lambda subject, hits, action: [i == 0 for i in range(len(hits))])
    with pytest.raises(ConformanceError, match="depended on the other hits"):
        check_authorizer(CachingAuthorizer(first_only))


class _AsyncAllowsAll(MetadataACLAuthorizer):
    async def acheck(self, subject, hits, action="read"):
        return [True] * len(hits)


class _AsyncWiderFilter(MetadataACLAuthorizer):
    async def afilter_for(self, subject, action="read"):
        return In("allowed_groups", ["*", *subject.groups])


@pytest.mark.parametrize(
    ("authorizer", "problem"),
    [
        (_AsyncAllowsAll(), "acheck and check disagree"),
        (_AsyncWiderFilter(), "afilter_for and filter_for disagree"),
        (CachingAuthorizer(_AsyncWiderFilter()), "afilter_for and filter_for disagree"),
    ],
)
def test_async_answers_must_agree_with_the_sync_ones(authorizer, problem):
    # aretrieve asks acheck and afilter_for; the kit once checked only check and filter_for.
    with pytest.raises(ConformanceError, match=problem):
        check_authorizer(authorizer)


def test_check_authorizer_accepts_your_own_subjects_and_hits():
    by_id = FunctionAuthorizer(
        check=lambda subject, hits, action: [h.metadata.get("owner") == subject.id for h in hits],
        filter_for=lambda subject, action: In("owner", {subject.id}),
    )
    hits = [Hit(f"d{i}", "text", 1.0, {"owner": owner}) for i, owner in enumerate(["alice", "bob", "alice"])]
    check_authorizer(by_id, [Subject("alice"), Subject("bob"), Subject("carol")], hits)


def test_hashing_embedder_is_deterministic_and_normalised():
    embed = HashingEmbedder(dim=8)
    a, b = embed("leave policy"), embed("leave policy")
    assert a == b and len(a) == 8
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
    with pytest.raises(ValueError):
        HashingEmbedder(dim=1)


class _MergingBackend(InMemoryBackend):
    """Merges new metadata into the old, as a partial update would: old permission keys survive."""

    def set_metadata(self, metadata):
        super().set_metadata({i: {**self._docs[i].metadata, **m} for i, m in metadata.items() if i in self._docs})


def test_set_metadata_that_keeps_old_keys_is_caught():
    with pytest.raises(ConformanceError, match="after set_metadata"):
        check_backend(_MergingBackend)


class _CreatingBackend(InMemoryBackend):
    """Stores ids it was never given, from metadata alone."""

    def set_metadata(self, metadata):
        for doc_id, m in metadata.items():
            self._docs[doc_id] = Document(doc_id, self._docs[doc_id].text if doc_id in self._docs else "doc", m)


def test_set_metadata_that_stores_unknown_ids_is_caught():
    with pytest.raises(ConformanceError, match="should not match"):
        check_backend(_CreatingBackend)
