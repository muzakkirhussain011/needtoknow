"""Haystack integration. Install with `pip install "wardowl[haystack]"`.

`WardowlRetriever` is a Haystack component that returns only the documents a subject may read:

    pipeline = Pipeline()
    pipeline.add_component("retriever", WardowlRetriever(PermissionedRetriever(backend, authorizer)))
    ...
    pipeline.run({"retriever": {"query": question, "subject": subject}})

The subject is a run input, so one pipeline serves every user: the application passes the
authenticated user on each run, as a `Subject` or a function returning one. Build it from your
session, never from the query or a model's output (see the security model). The component outputs
`documents` (Haystack `Document`s with the chunk id, text, score, and the metadata keys named in
`metadata_fields`, none by default) and `complete` (False when wardowl knows results may be
missing). It has `run_async` for `Pipeline.run_async`. Any authorizer failure raises
`AuthorizationError`; nothing is returned unfiltered.

A model calling the component as a tool cannot set the subject. `ComponentTool` leaves the
`subject` input out of the tool's parameters, and a value a model sends for it anyway reaches the
component as plain JSON, never as a `Subject`, so the call fails with nothing retrieved. In an
agent, give the tool the subject from the agent's state:
`ComponentTool(component=WardowlRetriever(...), inputs_from_state={"subject": "subject"})`.

`BoundWardowlRetriever` takes the subject from a function instead, such as one reading a
contextvar your web layer sets for the request, and has no `subject` input at all.

Connect a text embedder's `embedding` to the `query_embedding` input to search with that vector
instead of having the backend embed the query again. Name the sockets when you connect to
`WardowlRetriever`: its `subject` input accepts any type, so a connection by component name alone
is ambiguous.

The components hold live connections, so a pipeline containing one cannot be serialized to YAML.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from haystack import Document, component

from wardowl.retriever import PermissionedRetriever, RetrievalResult
from wardowl.types import Query, Subject

__all__ = ["BoundWardowlRetriever", "WardowlRetriever"]


def _query(text: str, embedding: list[float] | None) -> str | Query:
    return text if embedding is None else Query(text=text or None, vector=embedding)


def _output(result: RetrievalResult, fields: Collection[str]) -> dict[str, Any]:
    documents = [
        Document(id=h.id, content=h.text, meta=_picked(h.metadata, fields), score=h.score) for h in result.hits
    ]
    return {"documents": documents, "complete": result.complete}


def _picked(metadata: Mapping[str, Any], fields: Collection[str]) -> dict[str, Any]:
    return {key: metadata[key] for key in fields if key in metadata}


def _checked(subject: Any) -> Subject:
    if not isinstance(subject, Subject):
        raise TypeError("subject must be a wardowl Subject; retrieval never runs unfiltered")
    return subject


def _given(subject: Any) -> Subject:
    """The run's `subject` if it is a `Subject`, or what it returns if it is a function.

    A model's tool call carries only JSON, which is never a function and never a `Subject`.
    """
    return _checked(subject() if callable(subject) else subject)


def _fields(metadata_fields: Collection[str]) -> tuple[str, ...]:
    if isinstance(metadata_fields, str):
        raise TypeError("metadata_fields is a collection of metadata keys, not a single string")
    return tuple(metadata_fields)


@component
class WardowlRetriever:
    """Returns at most `top_k` documents the run's `subject` may read, best first.

    `subject` is the authenticated user's `Subject`, or a function returning it, called on every
    run. Anything else raises `TypeError`. The input is typed `Callable[[], Subject] | Any` on
    purpose. `ComponentTool` leaves an input whose type holds a function out of the tool's
    parameters, but it passes on every argument a model sends, converted to the input's type with
    pydantic, and an input typed `Subject` would turn a JSON object into one. With `Any`, the JSON
    stays as it is and is refused, so a model cannot name another user.

    Documents carry the chunk's metadata keys named in `metadata_fields`, none by default: permission
    fields such as `allowed_users`, `allowed_groups` and `tenant` say who may read what, and
    documents reach the model, prompts and traces.
    """

    def __init__(
        self, retriever: PermissionedRetriever, *, top_k: int = 5, metadata_fields: Collection[str] = ()
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self.retriever = retriever
        self.top_k = top_k
        self.metadata_fields = _fields(metadata_fields)

    @component.output_types(documents=list[Document], complete=bool)
    def run(
        self,
        query: str,
        subject: Callable[[], Subject] | Any,
        top_k: int | None = None,
        query_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Retrieve for `subject`. `top_k` overrides the component's default for this run.

        `query_embedding` (from a text embedder, for example) is searched with instead of embedding
        `query` again; `query` may then be empty.
        """
        q = _query(query, query_embedding)
        result = self.retriever.retrieve(q, _given(subject), top_k or self.top_k)
        return _output(result, self.metadata_fields)

    @component.output_types(documents=list[Document], complete=bool)
    async def run_async(
        self,
        query: str,
        subject: Callable[[], Subject] | Any,
        top_k: int | None = None,
        query_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """The async form of `run`, with the same inputs and outputs."""
        q = _query(query, query_embedding)
        result = await self.retriever.aretrieve(q, _given(subject), top_k or self.top_k)
        return _output(result, self.metadata_fields)


@component
class BoundWardowlRetriever:
    """Returns at most `top_k` documents the subject from `subject_provider()` may read, best first.

    The subject is not an input: `subject_provider` is called on every run, where the run happens
    (in the run's thread, or its task for `run_async`), and must return the authenticated user's
    `Subject`, for example by reading a contextvar the web layer set for the request. Neither a
    pipeline's caller nor a model calling the component as a tool can name another user. A
    provider that fails, or returns anything but a `Subject`, stops the run. Inputs, outputs and
    `metadata_fields` are as for `WardowlRetriever`, without `subject`.
    """

    def __init__(
        self,
        retriever: PermissionedRetriever,
        subject_provider: Callable[[], Subject],
        *,
        top_k: int = 5,
        metadata_fields: Collection[str] = (),
    ) -> None:
        provider: Any = subject_provider
        if not callable(provider):
            raise TypeError("subject_provider must be a function returning the authenticated user's Subject")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self.retriever = retriever
        self.subject_provider = subject_provider
        self.top_k = top_k
        self.metadata_fields = _fields(metadata_fields)

    @component.output_types(documents=list[Document], complete=bool)
    def run(self, query: str, top_k: int | None = None, query_embedding: list[float] | None = None) -> dict[str, Any]:
        """Retrieve for the subject `subject_provider` returns. `top_k` overrides the default for this run."""
        q = _query(query, query_embedding)
        result = self.retriever.retrieve(q, _checked(self.subject_provider()), top_k or self.top_k)
        return _output(result, self.metadata_fields)

    @component.output_types(documents=list[Document], complete=bool)
    async def run_async(
        self, query: str, top_k: int | None = None, query_embedding: list[float] | None = None
    ) -> dict[str, Any]:
        """The async form of `run`, with the same inputs and outputs."""
        q = _query(query, query_embedding)
        result = await self.retriever.aretrieve(q, _checked(self.subject_provider()), top_k or self.top_k)
        return _output(result, self.metadata_fields)
