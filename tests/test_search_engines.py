"""Elasticsearch and OpenSearch: the shared type-tagged terms, and the backends against real servers."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from wardowl import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, RankFusion, Subject
from wardowl.backends._engines import TERMS_FIELD, EngineBackend, _raise_for_items, filter_to_query, tag, terms
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
ES_URL = os.environ.get("WARDOWL_TEST_ELASTICSEARCH_URL")
OS_URL = os.environ.get("WARDOWL_TEST_OPENSEARCH_URL")


# ---- terms and translation (no server) ---------------------------------------------------------


def test_terms_tag_each_string_and_integer_with_its_type():
    metadata = {"allowed_groups": ["hr", "eng", "hr"], "level": 1, "tags": [1, "1", None], "flag": True, "score": 0.5}
    # Booleans, floats and None never match a filter, so they get no terms.
    assert terms("d", metadata) == sorted(
        [tag("allowed_groups", "eng"), tag("allowed_groups", "hr"), tag("level", 1), tag("tags", 1), tag("tags", "1")]
    )
    assert tag("level", 1) != tag("level", "1")


def test_filters_become_terms_queries_on_the_tags():
    a, b = {"terms": {TERMS_FIELD: [tag("a", "x")]}}, {"terms": {TERMS_FIELD: [tag("b", "y")]}}
    assert filter_to_query(In("team", ["b", 1])) == {"terms": {TERMS_FIELD: [tag("team", 1), tag("team", "b")]}}
    assert filter_to_query(And(In("a", ["x"]), In("b", ["y"]))) == {"bool": {"filter": [a, b]}}
    either = {"bool": {"should": [a, b], "minimum_should_match": 1}}
    assert filter_to_query(Or(In("a", ["x"]), In("b", ["y"]))) == either


def test_an_empty_in_matches_nothing_without_asking_the_engine():
    assert filter_to_query(In("team", [])) is None
    assert filter_to_query(And(In("a", ["x"]), In("b", []))) is None
    assert filter_to_query(Or(In("a", []), In("b", ["y"]))) == {"terms": {TERMS_FIELD: [tag("b", "y")]}}


def test_names_containing_the_separator_are_refused():
    with pytest.raises(ValueError):
        terms("d", {"a\x1fb": "x"})
    with pytest.raises(ValueError):
        filter_to_query(In("a\x1fb", ["x"]))


def test_bulk_errors_raise_except_deleting_what_is_not_there():
    missing = {"errors": True, "items": [{"delete": {"_id": "a", "status": 404}}]}
    _raise_for_items(missing, allow_not_found=True)
    with pytest.raises(RuntimeError, match="'a'"):
        _raise_for_items(missing, allow_not_found=False)
    _raise_for_items({"errors": False, "items": []}, allow_not_found=False)


def test_wrong_configuration_is_rejected():
    with pytest.raises(ValueError):
        EngineBackend(None, "", EMBED)
    with pytest.raises(ValueError):
        EngineBackend(None, "i", EMBED, num_candidates=0)
    assert EngineBackend(None, "i", EMBED).search("anything", 0) == []
    with pytest.raises(ValueError):
        EngineBackend(None, "i", EMBED, mode="semantic")


class Recorder:
    """A client that records search requests and bulk writes, and finds nothing."""

    def __init__(self):
        self.searches, self.bulks = [], []

    def search(self, **request):
        self.searches.append(request)
        return {"hits": {"hits": []}}

    def bulk(self, **request):
        self.bulks.append(request)
        return {"errors": False, "items": []}


def test_keyword_mode_sends_a_bm25_query_under_the_same_filter_and_needs_no_embed():
    from wardowl.backends.elasticsearch import ElasticsearchBackend
    from wardowl.backends.opensearch import OpenSearchBackend

    terms_filter = {"terms": {TERMS_FIELD: [tag("team", "hr")]}}
    es, os_client = Recorder(), Recorder()
    ElasticsearchBackend(es, "docs", None, mode="keyword").search("leave policy", 3, In("team", ["hr"]))
    OpenSearchBackend(os_client, "docs", None, mode="keyword").search("leave policy", 3)
    (es_request,) = es.searches
    assert es_request["query"] == {"bool": {"must": [{"match": {"content": "leave policy"}}], "filter": [terms_filter]}}
    assert es_request["size"] == 3 and es_request["source_excludes"] == ["embedding"]
    (os_request,) = os_client.searches
    assert os_request["body"]["query"] == {"bool": {"must": [{"match": {"content": "leave policy"}}], "filter": []}}
    # A keyword-only backend writes chunks without vectors; a vector backend refuses to without embed.
    ElasticsearchBackend(es, "docs", None, mode="keyword").add([Document("a", "leave", {"allowed_groups": ["*"]})])
    _, source = es.bulks[0]["operations"]
    assert "embedding" not in source and source["content"] == "leave"
    with pytest.raises(ValueError):
        ElasticsearchBackend(es, "docs", None).add([Document("a", "leave", {})])
    with pytest.raises(ValueError):
        ElasticsearchBackend(es, "docs", None).search("leave", 3)


# ---- real servers ------------------------------------------------------------------------------


def _elasticsearch():
    if not ES_URL:
        pytest.skip("set WARDOWL_TEST_ELASTICSEARCH_URL to run against Elasticsearch")
    es = pytest.importorskip("elasticsearch")
    from wardowl.backends.elasticsearch import ElasticsearchBackend, create_index, permission_terms

    def write_raw(client, index, doc_id, document):
        client.index(index=index, id=doc_id, document=document, refresh="wait_for")

    return {
        "client": es.Elasticsearch(ES_URL),
        "backend": ElasticsearchBackend,
        "create": create_index,
        "terms": permission_terms,
        "write_raw": write_raw,
        "make_async": lambda: es.AsyncElasticsearch(ES_URL),
    }


def _opensearch():
    if not OS_URL:
        pytest.skip("set WARDOWL_TEST_OPENSEARCH_URL to run against OpenSearch")
    opensearchpy = pytest.importorskip("opensearchpy")
    from wardowl.backends.opensearch import OpenSearchBackend, create_index, permission_terms

    def write_raw(client, index, doc_id, document):
        client.index(index=index, id=doc_id, body=document, refresh="wait_for")

    return {
        "client": opensearchpy.OpenSearch(hosts=[OS_URL]),
        "backend": OpenSearchBackend,
        "create": create_index,
        "terms": permission_terms,
        "write_raw": write_raw,
        "make_async": lambda: opensearchpy.AsyncOpenSearch(hosts=[OS_URL]),
    }


@pytest.fixture(params=["elasticsearch", "opensearch"])
def engine(request):
    return _elasticsearch() if request.param == "elasticsearch" else _opensearch()


def _new_index(engine):
    name = f"ntk-{uuid.uuid4().hex}"
    engine["create"](engine["client"], name, EMBED.dim)
    return name


def test_server_passes_the_conformance_kit(engine):
    previous: list[str] = []

    def make(docs):
        # A new index per example keeps examples independent.
        for name in previous:
            engine["client"].indices.delete(index=name)
        previous[:] = [_new_index(engine)]
        backend = engine["backend"](engine["client"], previous[0], EMBED)
        backend.add(docs)
        return backend

    check_backend(make)


def test_documents_written_by_another_tool_need_their_permission_terms(engine):
    name = _new_index(engine)
    client, backend = engine["client"], engine["backend"](engine["client"], name, EMBED)
    metadata = {"allowed_groups": ["hr"]}
    raw = {"content": "leave policy", "metadata": metadata, "embedding": EMBED("leave policy")}
    engine["write_raw"](client, name, "raw-1", raw)
    hr = Subject("ann", ["hr"])
    prefilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    postfilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="postfilter")
    # Without terms the pre-filter cannot see it; post-filtering still checks its metadata.
    assert prefilter.retrieve("leave", hr, k=5).hits == []
    assert [h.id for h in postfilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    engine["write_raw"](client, name, "raw-1", raw | {TERMS_FIELD: engine["terms"](metadata)})
    assert [h.id for h in prefilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    client.indices.delete(index=name)


def test_delete_removes_chunks_and_ignores_missing_ones(engine):
    name = _new_index(engine)
    backend = engine["backend"](engine["client"], name, EMBED)
    backend.add([Document("a", "leave policy", {"allowed_groups": ["*"]}), Document("b", "salary", {})])
    backend.delete(["a", "never-written"])
    assert [h.id for h in backend.search("leave policy salary", 5)] == ["b"]
    engine["client"].indices.delete(index=name)


def test_native_async_search_matches_sync(engine):
    name = _new_index(engine)
    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    engine["backend"](engine["client"], name, EMBED).add(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async_client = engine["make_async"]()
        try:
            backend = engine["backend"](engine["client"], name, EMBED, async_client=async_client)
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await async_client.close()

    async_result, sync_result = asyncio.run(run())
    engine["client"].indices.delete(index=name)
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups(engine):
    name = _new_index(engine)
    backend = engine["backend"](engine["client"], name, EMBED)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    engine["client"].indices.delete(index=name)
    assert [h.id for h in result.hits] == ["mine"]


def test_keyword_mode_passes_the_conformance_kit(engine):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            engine["client"].indices.delete(index=name)
        previous[:] = [_new_index(engine)]
        engine["backend"](engine["client"], previous[0], EMBED).add(docs)
        return engine["backend"](engine["client"], previous[0], None, mode="keyword")

    check_backend(make)
    for name in previous:
        engine["client"].indices.delete(index=name)


def test_hybrid_search_over_one_index_passes_the_conformance_kit(engine):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            engine["client"].indices.delete(index=name)
        previous[:] = [_new_index(engine)]
        vector = engine["backend"](engine["client"], previous[0], EMBED)
        vector.add(docs)
        return RankFusion(vector, engine["backend"](engine["client"], previous[0], None, mode="keyword"))

    check_backend(make)
    for name in previous:
        engine["client"].indices.delete(index=name)


def test_hybrid_search_with_one_query_vector_and_no_embed_function(engine):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            engine["client"].indices.delete(index=name)
        previous[:] = [_new_index(engine)]
        vector = engine["backend"](engine["client"], previous[0])
        vector.add(docs, [EMBED(d.text) for d in docs])
        return RankFusion(vector, engine["backend"](engine["client"], previous[0], mode="keyword"))

    check_backend(make, examples=8, query=Query(QUERY, EMBED(QUERY)))
    for name in previous:
        engine["client"].indices.delete(index=name)
