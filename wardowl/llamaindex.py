"""LlamaIndex integration. Install with `pip install "wardowl[llamaindex]"`.

* `WardowlRetriever`: a LlamaIndex retriever for one subject, usable in query engines and
  chat engines (`RetrieverQueryEngine.from_args(WardowlRetriever(permissioned, subject))`).
* `PermissionNodePostprocessor`: drops nodes the subject may not read, for existing pipelines.
  It post-filters, so it can return fewer nodes than were retrieved. The nodes it keeps are copies
  that carry only the metadata named in `metadata_fields`, like the retriever's.
* `LlamaIndexBackend`: an existing LlamaIndex index as a wardowl backend (post-filtered).

Any authorizer failure raises `AuthorizationError`; nothing is returned unfiltered.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.retrievers import BaseRetriever
from llama_index.core.schema import BaseNode, NodeWithScore, QueryBundle, RelatedNodeInfo, TextNode
from pydantic import ConfigDict

from wardowl.authz import AuthorizationError
from wardowl.filters import Filter
from wardowl.retriever import PermissionedRetriever, RetrievalResult
from wardowl.types import Hit, Query, Subject

__all__ = ["LlamaIndexBackend", "PermissionNodePostprocessor", "WardowlRetriever"]


def _query(bundle: QueryBundle) -> str | Query:
    """The bundle's text, with its embedding when LlamaIndex already computed one."""
    if bundle.embedding is None:
        return bundle.query_str
    return Query(text=bundle.query_str or None, vector=bundle.embedding)


def _nodes(result: RetrievalResult, fields: Collection[str]) -> list[NodeWithScore]:
    return [
        NodeWithScore(node=TextNode(id_=h.id, text=h.text, metadata=_picked(h.metadata, fields)), score=h.score)
        for h in result.hits
    ]


def _picked(metadata: Mapping[str, Any], fields: Collection[str]) -> dict[str, Any]:
    return {key: metadata[key] for key in fields if key in metadata}


def _trimmed(node: BaseNode, fields: Collection[str]) -> BaseNode:
    """A copy of `node` holding only the metadata keys named, in its relationships too.

    A relationship carries the metadata of the node it points to, such as the source document's
    permission fields. The copy is shallow: `node` itself is not changed.
    """
    relationships = {
        kind: [_related(r, fields) for r in related] if isinstance(related, list) else _related(related, fields)
        for kind, related in node.relationships.items()
    }
    return node.model_copy(update={"metadata": _picked(node.metadata, fields), "relationships": relationships})


def _related(info: RelatedNodeInfo, fields: Collection[str]) -> RelatedNodeInfo:
    return info.model_copy(update={"metadata": _picked(info.metadata, fields)})


class WardowlRetriever(BaseRetriever):
    """Returns only the nodes `subject` may read.

    Each node carries the chunk's metadata keys named in `metadata_fields`, none by default:
    permission fields such as `allowed_users`, `allowed_groups` and `tenant` say who may read what,
    and nodes reach the model, prompts and traces.
    """

    def __init__(
        self,
        retriever: PermissionedRetriever,
        subject: Subject,
        *,
        k: int = 4,
        metadata_fields: Collection[str] = (),
        **kwargs: Any,
    ) -> None:
        if subject is None:
            raise ValueError("a subject is required; retrieval never runs unfiltered")
        if isinstance(metadata_fields, str):
            raise TypeError("metadata_fields is a collection of metadata keys, not a single string")
        self._permissioned = retriever
        self._subject = subject
        self._k = k
        self._fields = tuple(metadata_fields)
        super().__init__(**kwargs)

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        return _nodes(self._permissioned.retrieve(_query(query_bundle), self._subject, self._k), self._fields)

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        return _nodes(await self._permissioned.aretrieve(_query(query_bundle), self._subject, self._k), self._fields)


class PermissionNodePostprocessor(BaseNodePostprocessor):
    """Keeps only the nodes `subject` may read, as decided by `authorizer`.

    The nodes it keeps are copies that carry the metadata keys named in `metadata_fields`, none by
    default, as the retriever's nodes do: LlamaIndex puts a node's metadata into prompts by default,
    and permission fields such as `allowed_users`, `allowed_groups` and `tenant` say who may read
    what. The authorizer sees all the metadata, and the nodes passed in are not changed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    authorizer: Any
    subject: Any
    action: str = "read"
    metadata_fields: tuple[str, ...] = ()

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
        except Exception as exc:  # fail closed, with a fixed message: the details stay in __cause__
            raise AuthorizationError("authorizer failed") from exc
        if len(decisions) != len(nodes) or not all(isinstance(d, bool) for d in decisions):
            raise AuthorizationError("authorizer must return one boolean per node")
        return [
            NodeWithScore(node=_trimmed(n.node, self.metadata_fields), score=n.score)
            for n, ok in zip(nodes, decisions, strict=True)
            if ok
        ]


class LlamaIndexBackend:
    """A LlamaIndex index (anything with `as_retriever(similarity_top_k=...)`) as a backend."""

    supports_filters = False

    def __init__(self, index: Any) -> None:
        self.index = index

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        q = Query.of(query)
        bundle = q.keywords() if q.vector is None else QueryBundle(query_str=q.text or "", embedding=list(q.vector))
        nodes = self.index.as_retriever(similarity_top_k=k).retrieve(bundle)
        return [Hit(n.node.node_id, n.node.get_content(), n.score or 0.0, dict(n.node.metadata)) for n in nodes]
