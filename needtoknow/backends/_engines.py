"""Type-exact permission terms for search engines whose keyword fields compare strings.

Elasticsearch and OpenSearch store keyword values as strings, so a `terms` query for the integer
`1` also matches the string `"1"`, and how a metadata field is mapped depends on the first document
that used it. The reference semantics keep `1` and `"1"` apart and never match booleans or floats.

So the backends index one keyword field, `needtoknow_terms`, holding a tagged term for every string
or integer in the metadata (`allowed_groups\\x1fs:hr`, `level\\x1fi:1`), for scalar fields and for
each element of a list field alike. A filter becomes a `terms` query on those tags, which compares
type and value exactly. The original metadata is stored unindexed and returned as it was written,
so the retriever's re-check sees the true permissions, and arbitrary metadata never adds mappings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from needtoknow.filters import And, Filter, In, Or, _is_value, _ordered
from needtoknow.types import Document, Hit, Query

TERMS_FIELD = "needtoknow_terms"
_SEP = "\x1f"  # the ASCII unit separator: field names may not contain it
_LISTS = (list, tuple, set, frozenset)


def tag(field: str, value: str | int) -> str:
    return f"{field}{_SEP}{'i' if isinstance(value, int) else 's'}:{value}"


def terms(doc_id: str, metadata: Mapping[str, Any]) -> list[str]:
    """The tagged terms for one chunk's metadata, sorted and without duplicates."""
    found: set[str] = set()
    for key, value in metadata.items():
        if not isinstance(key, str) or not key or _SEP in key:
            raise ValueError(f"{doc_id}: metadata key {key!r} is not allowed (empty, or contains \\x1f)")
        items = value if isinstance(value, _LISTS) else (value,)
        found.update(tag(key, v) for v in items if _is_value(v))
    return sorted(found)


def filter_to_query(f: Filter) -> dict[str, Any] | None:
    """Translate a filter into a query on the tagged terms, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        return {"terms": {TERMS_FIELD: [tag(f.field, v) for v in values]}} if values else None
    if isinstance(f, And):
        parts = [filter_to_query(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else {"bool": {"filter": parts}}
    if isinstance(f, Or):
        kept = [p for p in (filter_to_query(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else {"bool": {"should": kept, "minimum_should_match": 1}}
    raise TypeError(f"not a filter: {f!r}")


# ---- the backend logic Elasticsearch and OpenSearch share ------------------------------------------

Embed = Callable[[str], Sequence[float]]
_BATCH = 500


class EngineBackend:
    """kNN search on a search engine, with the permission filter applied inside the kNN search.

    Subclasses say how a kNN request and a bulk request look on their engine.
    """

    supports_filters = True

    def __init__(
        self,
        client: Any,
        index: str,
        embed: Embed | None = None,
        *,
        vector_field: str = "embedding",
        content_field: str = "content",
        metadata_field: str = "metadata",
        num_candidates: int = 100,
        refresh: bool | str = "wait_for",
        mode: str = "vector",
        async_client: Any = None,
    ) -> None:
        if not index:
            raise ValueError("index must be a non-empty name")
        if num_candidates < 1:
            raise ValueError("num_candidates must be positive")
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        self.client = client
        self.async_client = async_client
        self.index = index
        self.embed = embed
        self.vector_field = vector_field
        self.content_field = content_field
        self.metadata_field = metadata_field
        self.num_candidates = num_candidates
        self.refresh = refresh
        self.mode = mode

    # Engine specifics -------------------------------------------------------------------------

    def _search_request(self, vector: list[float], k: int, query: dict[str, Any] | None) -> dict[str, Any]:
        raise NotImplementedError

    def _keyword_request(self, text: str, k: int, query: dict[str, Any] | None) -> dict[str, Any]:
        raise NotImplementedError

    def _bulk(self, client: Any, operations: list[dict[str, Any]]) -> Any:
        raise NotImplementedError

    # Writing ----------------------------------------------------------------------------------

    def _source(self, doc: Document, vector: Sequence[float] | None) -> dict[str, Any]:
        source: dict[str, Any] = {
            self.content_field: doc.text,
            self.metadata_field: dict(doc.metadata),
            TERMS_FIELD: terms(doc.id, doc.metadata),
        }
        if vector is not None:  # a keyword-only backend without embed writes no vector
            source[self.vector_field] = [float(x) for x in vector]
        return source

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        docs = list(documents)
        vectors: list[Sequence[float] | None]
        if embeddings is not None:
            vectors = list(embeddings)
        elif self.embed is not None:
            vectors = [self.embed(d.text) for d in docs]
        elif self.mode == "keyword":
            vectors = [None] * len(docs)  # a keyword-only index needs no vectors
        else:
            raise ValueError("pass embeddings, or give the backend an embed function")
        if len(vectors) != len(docs):
            raise ValueError("one embedding per document is required")
        sources = [self._source(d, v) for d, v in zip(docs, vectors, strict=True)]
        for start in range(0, len(docs), _BATCH):
            operations: list[dict[str, Any]] = []
            for doc, source in zip(docs[start : start + _BATCH], sources[start : start + _BATCH], strict=True):
                operations += [{"index": {"_index": self.index, "_id": doc.id}}, source]
            _raise_for_items(self._bulk(self.client, operations), allow_not_found=False)

    def delete(self, ids: Iterable[str]) -> None:
        operations = [{"delete": {"_index": self.index, "_id": i}} for i in ids]
        if operations:
            _raise_for_items(self._bulk(self.client, operations), allow_not_found=True)

    # Searching --------------------------------------------------------------------------------

    def _request(self, q: Query, k: int, f: Filter | None) -> dict[str, Any] | None:
        query = None
        if f is not None:
            query = filter_to_query(f)
            if query is None:
                return None  # nothing can match
        if self.mode == "keyword":
            return self._keyword_request(q.keywords(), k, query)
        return self._search_request(q.embedded(self.embed), k, query)

    def _hits(self, response: Any) -> list[Hit]:
        hits = []
        for h in response["hits"]["hits"]:
            source = h.get("_source") or {}
            text = source.get(self.content_field)
            metadata = source.get(self.metadata_field)
            # A document without a metadata object carries no permissions: the re-check denies it.
            hits.append(
                Hit(
                    str(h["_id"]),
                    text if isinstance(text, str) else "",
                    float(h["_score"]),
                    metadata if isinstance(metadata, dict) else {},
                )
            )
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        request = self._request(Query.of(query), min(k, _MAX_K), filter)
        if request is None:
            return []
        return self._hits(self.client.search(index=self.index, **request))

    async def asearch(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_client is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        request = await asyncio.to_thread(self._request, Query.of(query), min(k, _MAX_K), filter)
        if request is None:
            return []
        return self._hits(await self.async_client.search(index=self.index, **request))


_MAX_K = 10_000  # both engines cap k and num_candidates at 10,000


def _raise_for_items(response: Any, *, allow_not_found: bool) -> None:
    if not response.get("errors"):
        return
    for item in response.get("items", []):
        ((_, result),) = item.items()
        status = result.get("status", 0)
        if status >= 300 and not (allow_not_found and status == 404):
            raise RuntimeError(f"bulk request failed for {result.get('_id')!r}: {result.get('error')}")


def mappings(vector: dict[str, Any], vector_field: str, content_field: str, metadata_field: str) -> dict[str, Any]:
    """Index mappings: text, the vector, the tagged terms, and the metadata stored but not indexed."""
    return {
        # Fields other than these are kept in _source but never mapped, so metadata cannot add mappings.
        "dynamic": False,
        "properties": {
            content_field: {"type": "text"},
            metadata_field: {"type": "object", "enabled": False},
            TERMS_FIELD: {"type": "keyword"},
            vector_field: vector,
        },
    }
