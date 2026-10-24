"""Vespa backend: nearest-neighbor or BM25 search on Vespa, with the permission filter in the query.

Talks to Vespa's HTTP APIs (`/document/v1` to write, `/search/` to search) with httpx, the `vespa`
extra. Each chunk is a Vespa document holding its id, its text, its metadata as JSON (returned
exactly as written), and the tagged permission terms of `wardowl.backends._engines`, hex-encoded,
in an array attribute with fast-search. A filter becomes `wardowl_terms in (...)` conditions
joined with `and` and `or`, which Vespa applies inside the nearest-neighbor or BM25 search, so
pre-filtering is exact; and every value the filter puts into the YQL text is hex digits, which no
metadata value can turn into YQL.

The application package needs a schema with these fields: `schema(dim)` writes one, and
`examples/vespa` is a complete package. `approximate=True` (the default) searches the HNSW index;
`approximate=False` compares the query with every chunk the filter admits. Vespa limits a query to
400 hits unless the query profile raises `maxHits`, as the example package does.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal
from urllib.parse import quote

from wardowl.backends._engines import _SEP, tag, terms
from wardowl.backends._errors import required
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Embed, Hit, Query

__all__ = ["TERMS_FIELD", "VespaBackend", "filter_to_yql", "schema"]

TERMS_FIELD = "wardowl_terms"
ID_FIELD = "wardowl_id"
METADATA_FIELD = "wardowl_metadata"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _hex(term: str) -> str:
    return term.encode().hex()


def filter_to_yql(f: Filter) -> str:
    """Translate a filter into a YQL condition on the hex-encoded terms.

    Raises `NothingMatches` when nothing can match, as a query without the condition matches everything.
    """
    return required(_condition(f))


def _condition(f: Filter) -> str | None:
    """The YQL condition, or None when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        if not values:
            return None
        quoted = (f'"{_hex(tag(f.field, v))}"' for v in values)
        return f"{TERMS_FIELD} in ({', '.join(quoted)})"
    if isinstance(f, And):
        parts = [_condition(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else f"({' and '.join(p for p in parts if p)})"
    if isinstance(f, Or):
        kept = [p for p in (_condition(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else f"({' or '.join(kept)})"
    raise TypeError(f"not a filter: {f!r}")


def schema(dim: int, name: str = "wardowl") -> str:
    """A Vespa schema (`schemas/<name>.sd`) for chunks with `dim`-dimensional vectors."""
    if dim < 1:
        raise ValueError("dim must be positive")
    if not _NAME.match(name):
        raise ValueError(f"{name!r} is not a valid schema name")
    return f"""schema {name} {{
    document {name} {{
        field {ID_FIELD} type string {{
            indexing: attribute | summary
        }}
        field text type string {{
            indexing: index | summary
            index: enable-bm25
        }}
        field {METADATA_FIELD} type string {{
            indexing: summary
        }}
        field {TERMS_FIELD} type array<string> {{
            indexing: attribute
            attribute: fast-search
        }}
        field embedding type tensor<float>(x[{dim}]) {{
            indexing: attribute | index
            attribute {{
                distance-metric: angular
            }}
        }}
    }}
    rank-profile wardowl {{
        inputs {{
            query(q) tensor<float>(x[{dim}])
        }}
        first-phase {{
            expression: cos(distance(field, embedding))
        }}
    }}
    rank-profile wardowl_bm25 {{
        first-phase {{
            expression: bm25(text)
        }}
    }}
}}
"""


def _httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised without the extra installed
        raise ImportError('VespaBackend needs httpx: pip install "wardowl[vespa]"') from exc
    return httpx


class VespaBackend:
    """Chunks in a Vespa content cluster, searched with the permission filter in the YQL query.

    `url` is the container's endpoint (`http://localhost:8080`). `mode="keyword"` ranks by BM25 on
    the text instead, under the same filter. Pass `client`, an `httpx.Client`, for TLS client
    certificates, tokens or proxies (Vespa Cloud, say). Writes go `concurrency` at a time.
    Thread-safe.
    """

    supports_filters = True

    def __init__(
        self,
        url: str,
        embed: Embed | None,
        *,
        schema: str = "wardowl",
        namespace: str = "wardowl",
        mode: Literal["vector", "keyword"] = "vector",
        approximate: bool = True,
        client: Any = None,
        timeout: float = 10.0,
        concurrency: int = 8,
    ) -> None:
        if not _NAME.match(schema) or not _NAME.match(namespace):
            raise ValueError("schema and namespace must be plain names")
        if mode not in ("vector", "keyword"):
            raise ValueError("mode must be 'vector' or 'keyword'")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        self.url = url.rstrip("/")
        self.embed = embed
        self.schema = schema
        self.namespace = namespace
        self.mode = mode
        self.approximate = approximate
        self.timeout = timeout
        self.concurrency = concurrency
        self._owns_client = client is None
        self._client = client if client is not None else _httpx().Client(timeout=timeout)

    def close(self) -> None:
        """Close the HTTP client, if the backend created it."""
        if self._owns_client:
            self._client.close()

    def _document(self, doc_id: str) -> str:
        return f"{self.url}/document/v1/{self.namespace}/{self.schema}/docid/{quote(doc_id, safe='')}"

    def _each(self, method: str, items: Sequence[tuple[str, Any]], what: str) -> None:
        """Send one document request per (id, body), `concurrency` at a time; any failure raises."""
        ok = frozenset({200}) if method == "POST" else frozenset({200, 404})  # 404: not stored, ignored

        def run(item: tuple[str, Any]) -> None:
            response = self._client.request(method, self._document(item[0]), json=item[1])
            if response.status_code not in ok:
                raise RuntimeError(f"Vespa {what} failed: HTTP {response.status_code}: {response.text[:300]}")

        if not items:
            return
        if len(items) == 1:
            run(items[0])
            return
        with ThreadPoolExecutor(min(self.concurrency, len(items))) as pool:
            list(pool.map(run, items))

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Store chunks, replacing any with the same id. `embeddings` gives their vectors, in order."""
        docs = list(documents)
        if not docs:
            return
        with_vectors = embeddings is not None or self.embed is not None or self.mode == "vector"
        vectors: list[Sequence[float] | None] = (
            list(document_vectors(docs, embeddings, self.embed)) if with_vectors else [None] * len(docs)
        )
        latest = {d.id: i for i, d in enumerate(docs)}  # the last of repeated ids wins, as in order
        puts = []
        for i in latest.values():
            doc, vector = docs[i], vectors[i]
            fields: dict[str, Any] = {
                ID_FIELD: doc.id,
                "text": doc.text,
                METADATA_FIELD: json.dumps(dict(doc.metadata)),
                TERMS_FIELD: [_hex(t) for t in terms(doc.id, doc.metadata)],
            }
            if vector is not None:
                fields["embedding"] = {"values": [float(x) for x in vector]}
            puts.append((doc.id, {"fields": fields}))
        self._each("POST", puts, "write")

    def delete(self, ids: Iterable[str]) -> None:
        """Remove chunks by id. Ids not stored are ignored."""
        self._each("DELETE", [(i, None) for i in sorted(set(ids))], "delete")

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored."""
        updates = []
        for doc_id, new in metadata.items():
            fields = {
                METADATA_FIELD: {"assign": json.dumps(dict(new))},
                TERMS_FIELD: {"assign": [_hex(t) for t in terms(doc_id, new)]},
            }
            updates.append((doc_id, {"fields": fields}))
        self._each("PUT", updates, "update")

    def _body(self, query: Query, k: int, where: str | None) -> dict[str, Any]:
        if self.mode == "vector":
            vector = query.embedded(self.embed)
            clause = f"{{targetHits: {k}, approximate: {str(self.approximate).lower()}}}nearestNeighbor(embedding, q)"
            body: dict[str, Any] = {"ranking.profile": "wardowl", "input.query(q)": [float(x) for x in vector]}
        else:
            if query.text is None:
                raise ValueError("keyword search needs the query's text")
            clause = '({defaultIndex: "text", grammar: "any"}userInput(@query))'
            body = {"ranking.profile": "wardowl_bm25", "query": query.text}
        yql = f"select * from {self.schema} where {clause}" + (f" and {where}" if where else "")
        return {**body, "yql": yql, "hits": k, "timeout": f"{self.timeout}s"}

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        where = None
        if filter is not None:
            where = _condition(filter)
            if where is None:
                return []
        response = self._client.post(f"{self.url}/search/", json=self._body(Query.of(query), k, where))
        try:
            root = response.json()["root"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"Vespa search failed: HTTP {response.status_code}: {response.text[:300]}") from None
        if root.get("errors") or response.status_code != 200:
            raise RuntimeError(f"Vespa search failed: HTTP {response.status_code}: {root.get('errors')}")
        coverage = root.get("coverage") or {}
        if coverage.get("full") is False or any((coverage.get("degraded") or {}).values()):
            # Some content nodes did not answer, or answered after a timeout: partial results.
            raise RuntimeError(f"Vespa returned partial results: coverage {coverage}")
        hits = []
        for child in root.get("children", []):
            fields = child.get("fields", {})
            if not isinstance(fields.get(ID_FIELD), str):
                raise RuntimeError(f"Vespa returned a hit without {ID_FIELD}: {child.get('id')!r}")
            metadata = json.loads(fields.get(METADATA_FIELD) or "{}")
            hits.append(Hit(fields[ID_FIELD], fields.get("text", ""), float(child["relevance"]), metadata))
        return hits
