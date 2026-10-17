"""LangChain and LlamaIndex integrations. Each part skips when its framework is not installed."""

from __future__ import annotations

import asyncio

import pytest

from needtoknow import (
    AuthorizationError,
    Document,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    Subject,
)

DOCS = [
    Document("handbook", "leave policy for all staff", {"allowed_groups": ["*"]}),
    Document("salaries", "salary bands and leave payouts", {"allowed_groups": ["hr"]}),
    Document("runbook", "incident runbook and deploy steps", {"allowed_groups": ["eng"]}),
    Document("bob-review", "bob leave review notes", {"allowed_users": ["bob"]}),
]
BOB = Subject("bob", ["eng"])
ALICE = Subject("alice", ["hr"])


def permissioned() -> PermissionedRetriever:
    return PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())


def _boom(*_args):
    raise ConnectionError("permission service unreachable")


# ---- LangChain -------------------------------------------------------------------------------


@pytest.fixture
def lc():
    pytest.importorskip("langchain_core")
    import needtoknow.langchain as integration

    return integration


def test_langchain_retriever_returns_only_permitted_documents(lc):
    docs = lc.NeedToKnowRetriever(retriever=permissioned()).invoke("leave", subject=BOB)
    assert {d.id for d in docs} == {"handbook", "bob-review"}
    assert all(d.metadata["relevance_score"] > 0 for d in docs)


def test_langchain_bound_subject_sync_and_async(lc):
    retriever = lc.NeedToKnowRetriever(retriever=permissioned(), k=5).for_subject(ALICE)
    assert {d.id for d in retriever.invoke("leave")} == {"handbook", "salaries"}
    assert {d.id for d in asyncio.run(retriever.ainvoke("leave"))} == {"handbook", "salaries"}


def test_langchain_without_a_subject_refuses_to_run(lc):
    with pytest.raises(ValueError):
        lc.NeedToKnowRetriever(retriever=permissioned()).invoke("leave")


def test_any_langchain_vector_store_can_be_post_filtered(lc):
    from langchain_core.documents import Document as LCDocument
    from langchain_core.embeddings import DeterministicFakeEmbedding
    from langchain_core.vectorstores import InMemoryVectorStore

    store = InMemoryVectorStore(DeterministicFakeEmbedding(size=16))
    store.add_documents([LCDocument(id=d.id, page_content=d.text, metadata=dict(d.metadata)) for d in DOCS])
    result = PermissionedRetriever(lc.LangChainVectorStoreBackend(store), MetadataACLAuthorizer()).retrieve(
        "leave", BOB, k=5
    )
    assert result.strategy == "postfilter"
    assert {h.id for h in result.hits} == {"handbook", "runbook", "bob-review"}


# ---- LlamaIndex ------------------------------------------------------------------------------


@pytest.fixture
def li():
    pytest.importorskip("llama_index.core")
    import needtoknow.llamaindex as integration

    return integration


def test_llamaindex_retriever_returns_only_permitted_nodes(li):
    retriever = li.NeedToKnowRetriever(permissioned(), BOB, k=5)
    assert {n.node.node_id for n in retriever.retrieve("leave")} == {"handbook", "bob-review"}
    assert {n.node.node_id for n in asyncio.run(retriever.aretrieve("leave"))} == {"handbook", "bob-review"}


def test_llamaindex_postprocessor_drops_nodes_and_fails_closed(li):
    from llama_index.core.schema import NodeWithScore, TextNode

    nodes = [NodeWithScore(node=TextNode(id_=d.id, text=d.text, metadata=dict(d.metadata)), score=1.0) for d in DOCS]
    kept = li.PermissionNodePostprocessor(authorizer=MetadataACLAuthorizer(), subject=BOB).postprocess_nodes(nodes)
    assert {n.node.node_id for n in kept} == {"handbook", "runbook", "bob-review"}

    class Broken:
        check = staticmethod(_boom)

    with pytest.raises(AuthorizationError):
        li.PermissionNodePostprocessor(authorizer=Broken(), subject=BOB).postprocess_nodes(nodes)


def test_an_existing_llamaindex_index_can_be_post_filtered(li):
    from llama_index.core import VectorStoreIndex
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.schema import TextNode

    nodes = [TextNode(id_=d.id, text=d.text, metadata=dict(d.metadata)) for d in DOCS]
    index = VectorStoreIndex(nodes, embed_model=MockEmbedding(embed_dim=8))
    result = PermissionedRetriever(li.LlamaIndexBackend(index), MetadataACLAuthorizer()).retrieve("leave", BOB, k=5)
    assert result.strategy == "postfilter"
    assert {h.id for h in result.hits} == {"handbook", "runbook", "bob-review"}


def test_a_langchain_store_searches_with_a_query_vector(lc):
    from langchain_core.documents import Document as LCDocument
    from langchain_core.embeddings import DeterministicFakeEmbedding
    from langchain_core.vectorstores import InMemoryVectorStore

    embedding = DeterministicFakeEmbedding(size=16)
    store = InMemoryVectorStore(embedding)
    store.add_documents([LCDocument(id=d.id, page_content=d.text, metadata=dict(d.metadata)) for d in DOCS])
    backend = lc.LangChainVectorStoreBackend(store)
    by_text = [h.id for h in backend.search("leave", 4)]
    assert [h.id for h in backend.search(Query(vector=embedding.embed_query("leave")), 4)] == by_text
    result = PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve(
        Query(vector=embedding.embed_query("leave")), BOB, k=5
    )
    assert {h.id for h in result.hits} == {"handbook", "runbook", "bob-review"}


class _Recording:
    supports_filters = False

    def __init__(self):
        self.queries = []

    def search(self, query, k, filter=None):
        self.queries.append(query)
        return []


def test_llamaindex_passes_on_an_embedding_it_already_computed(li):
    from llama_index.core.schema import QueryBundle

    backend = _Recording()
    retriever = li.NeedToKnowRetriever(PermissionedRetriever(backend, MetadataACLAuthorizer()), BOB)
    retriever.retrieve(QueryBundle("leave", embedding=[1.0, 0.0]))
    retriever.retrieve("leave")
    assert backend.queries == [Query("leave", [1.0, 0.0]), "leave"]


def test_an_existing_llamaindex_index_searches_with_a_query_vector(li):
    from llama_index.core import VectorStoreIndex
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.schema import TextNode

    nodes = [TextNode(id_=d.id, text=d.text, metadata=dict(d.metadata)) for d in DOCS]
    index = VectorStoreIndex(nodes, embed_model=MockEmbedding(embed_dim=8))
    backend = li.LlamaIndexBackend(index)
    assert {h.id for h in backend.search(Query(vector=[0.5] * 8), 4)} == {d.id for d in DOCS}
