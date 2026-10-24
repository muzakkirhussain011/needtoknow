"""Haystack integration. Install with `pip install "wardowl[haystack]"`.

`WardowlRetriever` is a Haystack component that returns only the documents a subject may read:

    pipeline = Pipeline()
    pipeline.add_component("retriever", WardowlRetriever(PermissionedRetriever(backend, authorizer)))
    ...
    pipeline.run({"retriever": {"query": question, "subject": subject}})

The subject is a run input, so one pipeline serves every user: the application passes the
authenticated user on each run. Build it from your session, never from the query or a model's
output (see the security model). The component outputs `documents` (Haystack `Document`s with the
chunk id, text, metadata and score) and `complete` (False when wardowl knows results may be
missing). It has `run_async` for `Pipeline.run_async`. Any authorizer failure raises
`AuthorizationError`; nothing is returned unfiltered.

Connect a text embedder's `embedding` to the `query_embedding` input to search with that vector
instead of having the backend embed the query again.

The component holds live connections, so a pipeline containing it cannot be serialized to YAML.
"""

from __future__ import annotations

from typing import Any

from haystack import Document, component

from wardowl.retriever import PermissionedRetriever, RetrievalResult
from wardowl.types import Query, Subject

__all__ = ["WardowlRetriever"]


def _query(text: str, embedding: list[float] | None) -> str | Query:
    return text if embedding is None else Query(text=text or None, vector=embedding)


def _output(result: RetrievalResult) -> dict[str, Any]:
    documents = [Document(id=h.id, content=h.text, meta=dict(h.metadata), score=h.score) for h in result.hits]
    return {"documents": documents, "complete": result.complete}


@component
class WardowlRetriever:
    """Returns at most `top_k` documents the run's `subject` may read, best first."""

    def __init__(self, retriever: PermissionedRetriever, *, top_k: int = 5) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self.retriever = retriever
        self.top_k = top_k

    @staticmethod
    def _subject(subject: Any) -> Subject:
        if not isinstance(subject, Subject):
            raise TypeError("subject must be a wardowl Subject; retrieval never runs unfiltered")
        return subject

    @component.output_types(documents=list[Document], complete=bool)
    def run(
        self, query: str, subject: Subject, top_k: int | None = None, query_embedding: list[float] | None = None
    ) -> dict[str, Any]:
        """Retrieve for `subject`. `top_k` overrides the component's default for this run.

        `query_embedding` (from a text embedder, for example) is searched with instead of embedding
        `query` again; `query` may then be empty.
        """
        q = _query(query, query_embedding)
        return _output(self.retriever.retrieve(q, self._subject(subject), top_k or self.top_k))

    @component.output_types(documents=list[Document], complete=bool)
    async def run_async(
        self, query: str, subject: Subject, top_k: int | None = None, query_embedding: list[float] | None = None
    ) -> dict[str, Any]:
        """The async form of `run`, with the same inputs and outputs."""
        q = _query(query, query_embedding)
        return _output(await self.retriever.aretrieve(q, self._subject(subject), top_k or self.top_k))
