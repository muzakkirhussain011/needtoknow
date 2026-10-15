"""Documents, chunking and permission-aware hybrid retrieval.

Access control is enforced *inside* retrieval: the collection and sensitivity filters are part of
the query, so chunks a caller may not read are never ranked, never returned and never reach the
model. The gateway re-checks every returned chunk as a second line of defence.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from haris.identity import SENSITIVITY_LEVELS

RRF_K = 60  # standard reciprocal-rank-fusion constant

_STOPWORD_TEXT = """
    a an and are as at be by can do does for from has have how i in is it its me my of on or our please
    should so than that the their them then there these this to us was we what when where which who why
    will with you your
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_WORD = re.compile(r"[a-z0-9]{3,}")


def keywords(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for w in _WORD.findall(text.lower()):
        if w not in _STOPWORDS:
            seen.setdefault(w, None)
    return list(seen)


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    collection: str
    sensitivity: str
    source: str
    text: str

    def __post_init__(self) -> None:
        if self.sensitivity not in SENSITIVITY_LEVELS:
            raise ValueError(f"unknown sensitivity {self.sensitivity!r} in {self.doc_id}")


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    title: str
    collection: str
    sensitivity: int  # numeric level, see SENSITIVITY_LEVELS
    source: str
    text: str


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float


def chunk_document(doc: Document, max_chars: int = 900, overlap_paragraphs: int = 1) -> list[Chunk]:
    """Pack paragraphs into chunks of about max_chars, carrying the last paragraph forward for context."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", doc.text) if p.strip()]
    chunks: list[list[str]] = []
    current: list[str] = []
    for para in paragraphs:
        if current and sum(len(p) for p in current) + len(para) > max_chars:
            chunks.append(current)
            current = current[-overlap_paragraphs:] if overlap_paragraphs else []
        current.append(para)
    if current:
        chunks.append(current)
    level = SENSITIVITY_LEVELS[doc.sensitivity]
    out = []
    for i, paras in enumerate(chunks):
        cid = hashlib.sha1(f"{doc.doc_id}:{i}".encode(), usedforsecurity=False).hexdigest()[:16]
        out.append(Chunk(cid, doc.doc_id, doc.title, doc.collection, level, doc.source, "\n\n".join(paras)))
    return out


def load_markdown_documents(root: Path) -> list[Document]:
    """Markdown files with YAML front matter: title, collection, sensitivity."""
    docs = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
        if not match:
            raise ValueError(f"{path} has no front matter")
        meta = yaml.safe_load(match.group(1)) or {}
        docs.append(
            Document(
                doc_id=str(path.relative_to(root)).replace("\\", "/"),
                title=meta["title"],
                collection=meta["collection"],
                sensitivity=meta["sensitivity"],
                source=str(path.relative_to(root)).replace("\\", "/"),
                text=match.group(2).strip(),
            )
        )
    return docs


class DocumentStore(Protocol):
    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None: ...

    def search(
        self,
        query_text: str,
        query_embedding: Sequence[float],
        collections: Sequence[str],
        max_sensitivity: int,
        k: int,
    ) -> list[ScoredChunk]: ...

    def collections(self) -> dict[str, int]: ...


def _rrf(rankings: Iterable[list[str]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    return scores


class InMemoryDocumentStore:
    """Same semantics as the pgvector store; used for tests and the offline demo."""

    def __init__(self) -> None:
        self._chunks: dict[str, Chunk] = {}
        self._vectors: dict[str, list[float]] = {}

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None:
        for chunk, vec in zip(chunks, embeddings, strict=True):
            self._chunks[chunk.id] = chunk
            self._vectors[chunk.id] = list(vec)

    def search(self, query_text, query_embedding, collections, max_sensitivity, k):
        allowed = [
            c for c in self._chunks.values() if c.collection in set(collections) and c.sensitivity <= max_sensitivity
        ]
        if not allowed:
            return []

        def cosine(a: Sequence[float], b: Sequence[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return dot / (na * nb)

        by_vector = sorted(allowed, key=lambda c: cosine(query_embedding, self._vectors[c.id]), reverse=True)
        terms = keywords(query_text)
        overlap = {c.id: sum(1 for t in terms if t in c.text.lower() or t in c.title.lower()) for c in allowed}
        by_keyword = sorted((c for c in allowed if overlap[c.id] > 0), key=lambda c: overlap[c.id], reverse=True)
        fused = _rrf([[c.id for c in by_vector[: k * 4]], [c.id for c in by_keyword[: k * 4]]])
        best = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [ScoredChunk(self._chunks[cid], score) for cid, score in best]

    def collections(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self._chunks.values():
            counts[c.collection] = counts.get(c.collection, 0) + 1
        return counts


class PgVectorDocumentStore:
    """PostgreSQL + pgvector: HNSW vector search and full-text search fused with RRF, ACL in the WHERE clause."""

    def __init__(self, pool) -> None:  # psycopg_pool.ConnectionPool
        self._pool = pool

    def upsert(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None:
        rows = [
            (c.id, c.doc_id, c.collection, c.title, c.source, c.sensitivity, c.text, _vec(e))
            for c, e in zip(chunks, embeddings, strict=True)
        ]
        with self._pool.connection() as conn:
            conn.cursor().executemany(
                """
                INSERT INTO chunks (id, doc_id, collection, title, source, sensitivity, content, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)
                ON CONFLICT (id) DO UPDATE SET doc_id = EXCLUDED.doc_id, collection = EXCLUDED.collection,
                  title = EXCLUDED.title, source = EXCLUDED.source, sensitivity = EXCLUDED.sensitivity,
                  content = EXCLUDED.content, embedding = EXCLUDED.embedding
                """,
                rows,
            )

    def search(self, query_text, query_embedding, collections, max_sensitivity, k):
        if not collections:
            return []
        terms = keywords(query_text)
        tsquery = " | ".join(terms) if terms else None  # terms are [a-z0-9] only, so no tsquery syntax injection
        params = {
            "q": _vec(query_embedding),
            "cols": list(collections),
            "lvl": max_sensitivity,
            "n": k * 4,
            "k": k,
            "tsq": tsquery,
        }
        keyword_cte = (
            """
            , kw AS (
              SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, query) DESC) AS r
              FROM chunks, to_tsquery('english', %(tsq)s) AS query
              WHERE collection = ANY(%(cols)s) AND sensitivity <= %(lvl)s AND tsv @@ query
              ORDER BY ts_rank_cd(tsv, query) DESC LIMIT %(n)s
            )"""
            if tsquery
            else ", kw AS (SELECT NULL::text AS id, NULL::bigint AS r WHERE false)"
        )
        sql = f"""
            WITH vec AS (
              SELECT id, row_number() OVER (ORDER BY embedding <=> %(q)s::vector) AS r
              FROM chunks
              WHERE collection = ANY(%(cols)s) AND sensitivity <= %(lvl)s
              ORDER BY embedding <=> %(q)s::vector LIMIT %(n)s
            ){keyword_cte}
            , fused AS (
              SELECT id, sum(1.0 / ({RRF_K} + r)) AS score
              FROM (SELECT id, r FROM vec UNION ALL SELECT id, r FROM kw) u GROUP BY id
            )
            SELECT c.id, c.doc_id, c.title, c.collection, c.sensitivity, c.source, c.content, f.score
            FROM fused f JOIN chunks c USING (id)
            ORDER BY f.score DESC LIMIT %(k)s
        """  # noqa: S608 - only constants are interpolated; values are bound parameters
        with self._pool.connection() as conn, conn.transaction():
            # pgvector >= 0.8: keep scanning the HNSW index until enough rows pass the ACL filter.
            conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            rows = conn.execute(sql, params).fetchall()
        return [ScoredChunk(Chunk(r[0], r[1], r[2], r[3], r[4], r[5], r[6]), float(r[7])) for r in rows]

    def collections(self) -> dict[str, int]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT collection, count(*) FROM chunks GROUP BY collection").fetchall()
        return {r[0]: int(r[1]) for r in rows}


def _vec(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"
