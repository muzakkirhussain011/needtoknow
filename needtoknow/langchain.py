"""LangChain integration. Install with `pip install "needtoknow[langchain]"`.

`NeedToKnowRetriever` is a LangChain retriever, so it works anywhere a retriever does (chains,
agents, LangGraph tools). Pass the subject per call, or bind one:

    retriever = NeedToKnowRetriever(retriever=permissioned)
    docs = retriever.invoke("What changed in Q3 pricing?", subject=Subject("bob", ["sales"]))
    bobs = retriever.for_subject(Subject("bob", ["sales"]))

A call without a subject raises instead of returning unfiltered results.

`LangChainVectorStoreBackend` goes the other way: it lets any LangChain vector store act as a
needtoknow backend. Filter syntax differs between stores, so results are post-filtered.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForRetrieverRun, CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LCDocument
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from needtoknow.filters import Filter
from needtoknow.retriever import PermissionedRetriever, RetrievalResult
from needtoknow.types import Hit, Query, Subject

__all__ = ["LangChainVectorStoreBackend", "NeedToKnowRetriever"]


def _documents(result: RetrievalResult) -> list[LCDocument]:
    return [
        LCDocument(id=h.id, page_content=h.text, metadata={**h.metadata, "relevance_score": h.score})
        for h in result.hits
    ]


class NeedToKnowRetriever(BaseRetriever):
    """Returns only the chunks the subject may read."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    retriever: PermissionedRetriever
    k: int = 4
    subject: Subject | None = None

    def for_subject(self, subject: Subject) -> NeedToKnowRetriever:
        return self.model_copy(update={"subject": subject})

    def _subject(self, subject: Subject | None) -> Subject:
        chosen = subject or self.subject
        if chosen is None:
            raise ValueError("pass subject=... or bind one with for_subject(); retrieval never runs unfiltered")
        return chosen

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
        subject: Subject | None = None,
        k: int | None = None,
    ) -> list[LCDocument]:
        return _documents(self.retriever.retrieve(query, self._subject(subject), k or self.k))

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        run_manager: AsyncCallbackManagerForRetrieverRun,
        subject: Subject | None = None,
        k: int | None = None,
    ) -> list[LCDocument]:
        return _documents(await self.retriever.aretrieve(query, self._subject(subject), k or self.k))


class LangChainVectorStoreBackend:
    """Any LangChain `VectorStore` as a needtoknow backend (post-filtered).

    Documents need an id: `Document.id`, or the metadata field named by `id_key`. Scores follow
    the store's ranking (best first), since stores disagree on what their raw scores mean. A
    `Query` with a vector is searched with `similarity_search_by_vector`.
    """

    supports_filters = False

    def __init__(self, vectorstore: Any, *, id_key: str | None = None) -> None:
        self.vectorstore = vectorstore
        self.id_key = id_key

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        q = Query.of(query)
        if q.vector is not None:
            docs = self.vectorstore.similarity_search_by_vector(list(q.vector), k=k)
        else:
            docs = self.vectorstore.similarity_search(q.keywords(), k=k)
        hits = []
        for rank, doc in enumerate(docs):
            doc_id = doc.id if self.id_key is None else doc.metadata.get(self.id_key)
            if not doc_id:
                raise ValueError("every document needs an id (Document.id or the id_key metadata field)")
            hits.append(Hit(str(doc_id), doc.page_content, 1.0 / (1 + rank), dict(doc.metadata)))
        return hits
