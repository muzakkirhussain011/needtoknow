"""LlamaIndex integration. Install with `pip install "needtoknow[llamaindex]"`.

* `NeedToKnowRetriever`: a LlamaIndex retriever for one subject, usable in query engines and
  chat engines (`RetrieverQueryEngine.from_args(NeedToKnowRetriever(permissioned, subject))`).
* `PermissionNodePostprocessor`: drops nodes the subject may not read, for existing pipelines.
  It post-filters, so it can return fewer nodes than were retrieved.
* `LlamaIndexBackend`: an existing LlamaIndex index as a needtoknow backend (post-filtered).

Any authorizer failure raises `AuthorizationError`; nothing is returned unfiltered.
"""

from __future__ import annotations

from typing import Any

from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from pydantic import ConfigDict

from needtoknow.authz import AuthorizationError
from needtoknow.filters import Filter
from needtoknow.retriever import PermissionedRetriever, RetrievalResult
from needtoknow.types import Hit, Subject

__all__ = ["LlamaIndexBackend", "NeedToKnowRetriever", "PermissionNodePostprocessor"]


def _nodes(result: RetrievalResult) -> list[NodeWithScore]:
    return [
        NodeWithScore(node=TextNode(id_=h.id, text=h.text, metadata=dict(h.metadata)), score=h.score)
        for h in result.hits
    ]


class NeedToKnowRetriever(BaseRetriever):
    """Returns only the nodes `subject` may read."""

    def __init__(self, retriever: PermissionedRetriever, subject: Subject, *, k: int = 4, **kwargs: Any) -> None:
        if subject is None:
            raise ValueError("a subject is required; retrieval never runs unfiltered")
        self._permissioned = retriever
        self._subject = subject
        self._k = k
        super().__init__(**kwargs)

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        return _nodes(self._permissioned.retrieve(query_bundle.query_str, self._subject, self._k))

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        return _nodes(await self._permissioned.aretrieve(query_bundle.query_str, self._subject, self._k))


class PermissionNodePostprocessor(BaseNodePostprocessor):
    """Keeps only the nodes `subject` may read, as decided by `authorizer`."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    authorizer: Any
    subject: Any
    action: str = "read"

    @classmethod
    def class_name(cls) -> str:
        return "PermissionNodePostprocessor"

    def _postprocess_nodes(
        self, nodes: list[NodeWithScore], query_bundle: QueryBundle | None = None
    ) -> list[NodeWithScore]:
        if not nodes:
            return []
        hits = [Hit(n.node.node_id, n.node.get_content(), n.score or 0.0, dict(n.node.metadata)) for n in nodes]
        try:
            decisions = list(self.authorizer.check(self.subject, hits, self.action))
        except AuthorizationError:
            raise
        except Exception as exc:  # fail closed
            raise AuthorizationError(f"authorizer failed: {exc}") from exc
        if len(decisions) != len(nodes) or not all(isinstance(d, bool) for d in decisions):
            raise AuthorizationError("authorizer must return one boolean per node")
        return [n for n, ok in zip(nodes, decisions, strict=True) if ok]


class LlamaIndexBackend:
    """A LlamaIndex index (anything with `as_retriever(similarity_top_k=...)`) as a backend."""

    supports_filters = False

    def __init__(self, index: Any) -> None:
        self.index = index

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        nodes = self.index.as_retriever(similarity_top_k=k).retrieve(query)
        return [Hit(n.node.node_id, n.node.get_content(), n.score or 0.0, dict(n.node.metadata)) for n in nodes]
