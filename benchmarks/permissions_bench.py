"""Permission-aware retrieval benchmark with real embeddings.

For every backend, strategy, permission selectivity and permission layout it measures

* recall@k against the exact top-k of what the subject may read, computed by brute force over the
  same vectors and independently of needtoknow,
* leaks: returned chunks the subject may not read,
* how often the result is complete and how many candidates the authorizer checked,
* latency per retrieval. Query embedding is excluded: every query is embedded once, up front.

Permissions are hierarchical, as in most organisations: a chunk belongs to one team, every ten
teams form a department and every hundred a division, and its `allowed_groups` lists all three.
A subject's selectivity (the share of teams it may read) is expressed with the fewest groups, so
filters stay as small as real ones. Layouts (`--layouts`):

random      teams are random; subjects hold random groups.
aligned     teams are topical (chunks nearest the same anchor share a team); subjects hold the groups
            nearest their query, as when people search their own department's documents.
misaligned  topical teams; subjects hold the groups farthest from their query. This is the hard case
            for filtered approximate search.

Run it on Kaggle with a GPU for the embeddings (see benchmarks/README.md):

    python benchmarks/permissions_bench.py --dataset fiqa --embedder Qwen/Qwen3-Embedding-0.6B \\
        --backends exact,chroma,pgvector --database-url postgresql://...

`--backends ...,pgvector-rls` adds a baseline without needtoknow: PostgreSQL's own row-level security on
a copy of the pgvector table (see `pgvector_rls_run`).

`--dataset synthetic --embedder hashing` needs no model or network. With `--check` the run fails
unless no strategy leaks, nothing errors, and the exact backend's complete results are exact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

import needtoknow
from needtoknow import And, Document, Filter, Hit, In, MetadataACLAuthorizer, PermissionedRetriever, Subject

FIELD = "allowed_groups"
LEVELS = {1: "team", 10: "dept", 100: "div"}
LAYOUTS = ("random", "aligned", "misaligned")

# (returned ids, candidates examined, complete, violations)
Run = Callable[[str, Subject, int], tuple[list[str], int, bool, int]]


def log(message: str) -> None:
    print(message, flush=True)


# ---- corpus and embeddings -------------------------------------------------------------------


def load_fiqa(max_docs: int, n_queries: int, rng: np.random.Generator) -> tuple[list[str], list[str], list[str]]:
    from datasets import load_dataset

    corpus = load_dataset("BeIR/fiqa", "corpus", split="corpus")
    questions = load_dataset("BeIR/fiqa", "queries", split="queries")
    pairs = [
        (doc_id, f"{title}\n{text}".strip())
        for doc_id, title, text in zip(corpus["_id"], corpus["title"], corpus["text"], strict=True)
        if text.strip()
    ]
    if 0 < max_docs < len(pairs):
        pairs = [pairs[i] for i in np.sort(rng.choice(len(pairs), max_docs, replace=False))]
    unique = list(dict.fromkeys(q.strip() for q in questions["text"] if q.strip()))
    picked = rng.choice(len(unique), min(n_queries, len(unique)), replace=False)
    return [p[0] for p in pairs], [p[1] for p in pairs], [unique[i] for i in picked]


def load_synthetic(n_docs: int, n_queries: int, rng: np.random.Generator) -> tuple[list[str], list[str], list[str]]:
    topics = [[f"t{t}w{w}" for w in range(40)] for t in range(50)]
    noise = [f"n{w}" for w in range(500)]

    def text(n_topic: int, n_noise: int) -> str:
        topic = topics[int(rng.integers(len(topics)))]
        return " ".join([*rng.choice(topic, n_topic), *rng.choice(noise, n_noise)])

    docs = [text(12, 4) for _ in range(n_docs)]
    queries = list(dict.fromkeys(text(4, 0) for _ in range(n_queries * 2)))[:n_queries]
    return [f"d{i}" for i in range(n_docs)], docs, queries


def hashing_embed(texts: Sequence[str], rng: np.random.Generator, dim: int = 128) -> np.ndarray:
    """Bag-of-words vectors, with a little noise so that scores do not tie."""
    out = np.full((len(texts), dim), 1e-3, dtype=np.float32)
    for row, text in enumerate(texts):
        for token in re.findall(r"\w+", text.lower()):
            out[row, int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big") % dim] += 1.0
    out += rng.normal(0.0, 1e-3, out.shape).astype(np.float32)
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def model_embed(
    name: str, docs: Sequence[str], queries: Sequence[str], batch_size: int, query_prefix: str = ""
) -> tuple[np.ndarray, np.ndarray]:
    """Embed with a sentence-transformers model; `query_prefix` is for models that want one (BGE)."""
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(name, device=device)
    if device == "cuda":
        model.half()
    model.max_seq_length = min(model.max_seq_length or 512, 512)
    prompt = "query" if "query" in (model.prompts or {}) and not query_prefix else None
    queries = [query_prefix + q for q in queries]
    options = {"batch_size": batch_size, "normalize_embeddings": True, "convert_to_numpy": True}
    gpu = torch.cuda.get_device_name(0) if device == "cuda" else "no GPU"
    log(f"embedding {len(docs)} chunks and {len(queries)} queries with {name} on {device} ({gpu})")
    # Logged in slices, because a progress bar that redraws one line never reaches notebook logs.
    parts = []
    for start in range(0, len(docs), 5000):
        parts.append(model.encode(list(docs[start : start + 5000]), **options))
        log(f"embedded {min(start + 5000, len(docs))}/{len(docs)} chunks")
    query_vecs = model.encode(list(queries), prompt_name=prompt, **options)
    return np.concatenate(parts).astype(np.float32), query_vecs.astype(np.float32)


# ---- permissions -----------------------------------------------------------------------------


def tags(prefix: str, team: int) -> list[str]:
    return [f"{prefix}-{name}-{team // unit}" for unit, name in LEVELS.items()]


def nearest(vectors: np.ndarray, anchors: np.ndarray) -> np.ndarray:
    parts = np.array_split(vectors, max(1, len(vectors) // 4096))
    return np.concatenate([np.argmax(part @ anchors.T, axis=1) for part in parts])


@dataclass(frozen=True)
class Case:
    query: str
    subject: Subject
    permitted: np.ndarray
    truth: frozenset[str]


def subject_units(
    layout: str, teams: int, selectivity: float, sims: np.ndarray | None, rng: np.random.Generator
) -> tuple[int, np.ndarray]:
    """The fewest groups covering `selectivity` of the teams: (unit size, chosen unit indexes)."""
    target = max(1, round(selectivity * teams))
    unit = max(u for u in LEVELS if u <= target)
    count = max(1, round(target / unit))
    n_units = teams // unit
    if sims is None:
        return unit, rng.choice(n_units, count, replace=False)
    best = sims.reshape(n_units, unit).max(axis=1)
    order = np.argsort(-best, kind="stable")
    return unit, (order[:count] if layout == "aligned" else order[::-1][:count])


def top_rows(scores: np.ndarray, k: int) -> np.ndarray:
    k = min(k, int(np.isfinite(scores).sum()))
    if k <= 0:
        return np.empty(0, dtype=np.int64)
    rows = np.argpartition(-scores, k - 1)[:k]
    return rows[np.argsort(-scores[rows], kind="stable")]


# ---- backends --------------------------------------------------------------------------------


class ExactBackend:
    """Brute-force cosine search with exact filtering over normalised vectors."""

    supports_filters = True

    def __init__(self, ids: Sequence[str], vectors: np.ndarray, metadata: Sequence[dict[str, Any]], embed: Any):
        self.ids = list(ids)
        self.vectors = vectors
        self.metadata = list(metadata)
        self.embed = embed
        index: dict[str, dict[tuple[str, Any], list[int]]] = defaultdict(lambda: defaultdict(list))
        for row, md in enumerate(self.metadata):
            for key, value in md.items():
                for item in value if isinstance(value, (list, tuple, set, frozenset)) else [value]:
                    if isinstance(item, (str, int)) and not isinstance(item, bool):
                        index[key][(type(item).__name__, item)].append(row)
        self._index = {key: {v: np.asarray(rows) for v, rows in values.items()} for key, values in index.items()}

    def mask(self, f: Filter) -> np.ndarray:
        if isinstance(f, In):
            out = np.zeros(len(self.ids), dtype=bool)
            column = self._index.get(f.field, {})
            for value in f.values:
                rows = column.get((type(value).__name__, value))
                if rows is not None:
                    out[rows] = True
            return out
        parts = [self.mask(c) for c in f.clauses]
        return np.logical_and.reduce(parts) if isinstance(f, And) else np.logical_or.reduce(parts)

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        scores = self.vectors @ np.asarray(self.embed(query), dtype=np.float32)
        if filter is not None:
            scores = np.where(self.mask(filter), scores, -np.inf)
        return [Hit(self.ids[r], "", float(scores[r]), self.metadata[r]) for r in top_rows(scores, k)]


def documents(ids: Sequence[str], texts: Sequence[str], metadata: Sequence[dict[str, Any]]) -> list[Document]:
    return [Document(i, t, m) for i, t, m in zip(ids, texts, metadata, strict=True)]


def chroma_backend(docs: list[Document], vectors: np.ndarray, embed: Any, batch: int) -> tuple[Any, dict[str, str]]:
    import chromadb

    from needtoknow.backends.chroma import ChromaBackend

    collection = chromadb.EphemeralClient().create_collection(
        f"bench_{time.time_ns()}", embedding_function=None, configuration={"hnsw": {"space": "cosine"}}
    )
    backend = ChromaBackend(collection, embed=embed)
    for start in range(0, len(docs), batch):
        backend.add(docs[start : start + batch], embeddings=vectors[start : start + batch])
    return backend, {"chromadb": chromadb.__version__}


def pgvector_backends(
    url: str, docs: list[Document], vectors: np.ndarray, embed: Any, batch: int
) -> tuple[dict[str, Any], dict[str, str]]:
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    table = sql.Identifier("needtoknow_bench")
    pool = ConnectionPool(url, min_size=1, max_size=2, open=True)
    with pool.connection() as conn:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
    create_schema(pool, vectors.shape[1], table="needtoknow_bench", hnsw=False)
    loader = PgVectorBackend(pool, embed, table="needtoknow_bench")
    for start in range(0, len(docs), batch):
        loader.add(docs[start : start + batch], embeddings=vectors[start : start + batch])
    with pool.connection() as conn:
        conn.execute("SET maintenance_work_mem = '1GB'")
        conn.execute(sql.SQL("CREATE INDEX ON {} USING hnsw (embedding vector_cosine_ops)").format(table))
        conn.execute(sql.SQL("ANALYZE {}").format(table))
        server = conn.execute("SHOW server_version").fetchone()[0]
        extension = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
    backends = {
        "pgvector": PgVectorBackend(pool, embed, table="needtoknow_bench"),
        "pgvector, no iterative scan": PgVectorBackend(pool, embed, table="needtoknow_bench", iterative_scan=None),
    }
    return backends, {"postgres": server, "pgvector": extension}


RLS = "row-level security"
READER = "needtoknow_bench_reader"


def pgvector_rls_run(
    url: str, docs: list[Document], vectors: np.ndarray, embed: Any, batch: int
) -> tuple[Run, dict[str, str]]:
    """PostgreSQL's own row-level security: the baseline that needtoknow's pgvector backend replaces.

    A separate table holds the same chunks with the same HNSW index, and row-level security is
    forced on it with the policy most applications would write for a jsonb ACL: a row is visible
    when its `allowed_groups` holds one of the groups the request puts in `needtoknow.groups`. Each
    search sets that list for its own transaction, then asks for the k nearest rows with no filter
    of its own and the same iterative scan as needtoknow's backend, so the database alone decides
    what is visible. needtoknow plays no part in these results.

    Superusers, and roles with BYPASSRLS, skip row-level security even on a table that forces it,
    and the benchmark's database user is usually a superuser. So each search switches to a plain
    role that may only read this table, and setup fails unless that role sees nothing without groups.
    """
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    name = "needtoknow_bench_rls"
    table, reader = sql.Identifier(name), sql.Identifier(READER)
    pool = ConnectionPool(url, min_size=1, max_size=2, open=True)
    with pool.connection() as conn:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
    create_schema(pool, vectors.shape[1], table=name, hnsw=False)
    loader = PgVectorBackend(pool, embed, table=name)
    for start in range(0, len(docs), batch):
        loader.add(docs[start : start + batch], embeddings=vectors[start : start + batch])
    field = sql.Literal(FIELD)
    with pool.connection() as conn:
        conn.execute("SET maintenance_work_mem = '1GB'")
        conn.execute(sql.SQL("CREATE INDEX ON {} USING hnsw (embedding vector_cosine_ops)").format(table))
        # The default jsonb_ops class, unlike jsonb_path_ops, serves the policy's ?| test.
        conn.execute(sql.SQL("CREATE INDEX ON {} USING gin ((metadata -> {}))").format(table, field))
        conn.execute(sql.SQL("ANALYZE {}").format(table))
        conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
        conn.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(table))  # for the owner too
        # With no groups set, current_setting gives NULL and no row is visible.
        conn.execute(
            sql.SQL(
                "CREATE POLICY readable ON {} FOR SELECT"
                " USING (metadata -> {} ?| string_to_array(current_setting('needtoknow.groups', true), ','))"
            ).format(table, field)
        )
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [READER]).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(reader))
        conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(table, reader))
        conn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(reader))  # so this user may SET ROLE to it
        server = conn.execute("SHOW server_version").fetchone()[0]
        extension = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
    with pool.connection() as conn, conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(reader))
        visible = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()[0]
    if visible:
        raise RuntimeError(f"row-level security is not enforced: {visible} rows visible with no groups set")
    statement = sql.SQL(
        "WITH candidates AS MATERIALIZED ("
        " SELECT id, embedding <=> %s::vector AS distance FROM {} ORDER BY distance LIMIT %s)"
        " SELECT id FROM candidates ORDER BY distance, id"
    ).format(table)

    def run(query: str, subject: Subject, k: int) -> tuple[list[str], int, bool, int]:
        if any("," in group for group in subject.groups):
            raise ValueError("a group name with a comma cannot be passed in needtoknow.groups")
        vector = "[" + ",".join(f"{v:.8g}" for v in embed(query)) + "]"
        with pool.connection() as conn, conn.transaction():
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(reader))
            conn.execute("SELECT set_config('needtoknow.groups', %s, true)", [",".join(subject.groups)])
            conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            ids = [row[0] for row in conn.execute(statement, [vector, k]).fetchall()]
        # The database checks rows itself, so nothing is examined by the application.
        return ids, 0, len(ids) == k, 0

    return run, {"postgres": server, "pgvector": extension}


# ---- systems under test ----------------------------------------------------------------------


def needtoknow_run(backend: Any, authorizer: Any, strategy: str, max_candidates: int) -> Run:
    retriever = PermissionedRetriever(backend, authorizer, strategy=strategy, max_candidates=max_candidates)

    def run(query: str, subject: Subject, k: int) -> tuple[list[str], int, bool, int]:
        result = retriever.retrieve(query, subject, k)
        return [h.id for h in result.hits], result.examined, result.complete, result.violations

    return run


def naive_run(backend: Any, authorizer: Any) -> Run:
    """What hand-written RAG often does: take the top k, then drop what the subject may not read."""

    def run(query: str, subject: Subject, k: int) -> tuple[list[str], int, bool, int]:
        hits = backend.search(query, k)
        kept = [h.id for h, ok in zip(hits, authorizer.check(subject, hits), strict=True) if ok]
        return kept, len(hits), len(kept) == k, 0

    return run


def unprotected_run(backend: Any) -> Run:
    """No permission check at all: what a RAG system returns when retrieval ignores the user."""

    def run(query: str, subject: Subject, k: int) -> tuple[list[str], int, bool, int]:
        return [h.id for h in backend.search(query, k)], 0, True, 0

    return run


def systems(backends: dict[str, Any], max_candidates: int) -> list[tuple[str, str, Run]]:
    authz = MetadataACLAuthorizer()
    out: list[tuple[str, str, Run]] = []
    for name, backend in backends.items():
        if name == "pgvector, no iterative scan":
            out.append((name, "prefilter", needtoknow_run(backend, authz, "prefilter", max_candidates)))
            continue
        for strategy in ("prefilter", "postfilter"):
            out.append((name, strategy, needtoknow_run(backend, authz, strategy, max_candidates)))
        out.append((name, "naive top-k", naive_run(backend, authz)))
        if name == "exact":
            out.append((name, "unprotected", unprotected_run(backend)))
    return out


# ---- measurement -----------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else 0.0


def measure(run: Run, cases: Sequence[Case], k: int, row_of: dict[str, int], budget: float) -> dict[str, Any]:
    recalls: list[float] = []
    latencies: list[float] = []
    examined: list[int] = []
    leaks = complete = full = errors = violations = inexact_complete = 0
    first_error = None
    deadline = time.perf_counter() + budget
    for case in cases:
        if time.perf_counter() > deadline:
            break
        start = time.perf_counter()
        try:
            ids, seen, done, bad = run(case.query, case.subject, k)
        except Exception as exc:
            errors += 1
            first_error = first_error or f"{type(exc).__name__}: {exc}"[:300]
            continue
        latencies.append((time.perf_counter() - start) * 1000)
        recall = len(set(ids) & case.truth) / len(case.truth) if case.truth else 1.0
        recalls.append(recall)
        examined.append(seen)
        leaks += sum(1 for i in ids if not case.permitted[row_of[i]])
        complete += done
        full += recall == 1.0
        violations += bad
        inexact_complete += done and recall < 1.0
    n = len(recalls)
    return {
        "queries": n,
        "recall": round(statistics.fmean(recalls), 4) if n else 0.0,
        "full_recall_rate": round(full / n, 4) if n else 0.0,
        "complete_rate": round(complete / n, 4) if n else 0.0,
        "inexact_complete": inexact_complete,
        "examined": round(statistics.fmean(examined), 1) if n else 0.0,
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "leaks": leaks,
        "violations": violations,
        "errors": errors,
        "first_error": first_error,
    }


def problems(rows: Sequence[dict[str, Any]]) -> list[str]:
    found = []
    for r in rows:
        where = f"{r['layout']} {r['selectivity']} {r['backend']} {r['strategy']}"
        if r["strategy"] != "unprotected" and r["leaks"]:
            found.append(f"{where}: {r['leaks']} leaks")
        if r["errors"] or r["violations"]:
            found.append(f"{where}: {r['errors']} errors, {r['violations']} violations ({r['first_error']})")
        if r["backend"] == "exact" and r["strategy"] == "prefilter" and r["recall"] < 1.0:
            found.append(f"{where}: recall {r['recall']} from an exact pre-filtered search")
        if r["backend"] == "exact" and r["strategy"] != "unprotected" and r["inexact_complete"]:
            found.append(f"{where}: {r['inexact_complete']} results marked complete were not the exact top-k")
    return found


def markdown(rows: Sequence[dict[str, Any]], k: int) -> str:
    header = ["Selectivity", "Readable", "Backend", "Strategy", f"Recall@{k}", "Complete", "Examined"]
    header += ["p50 ms", "p95 ms", "Leaks"]
    out = []
    for layout in dict.fromkeys(r["layout"] for r in rows):
        out += [f"### Layout: {layout}", "", "| " + " | ".join(header) + " |", "|---" * len(header) + "|"]
        for r in (r for r in rows if r["layout"] == layout):
            cells = [f"{r['selectivity']:.1%}", f"{r['readable']:.2%}", r["backend"], r["strategy"]]
            examined = "n/a" if r["strategy"] == RLS else f"{r['examined']:g}"  # checked inside the database
            cells += [f"{r['recall']:.3f}", f"{r['complete_rate']:.0%}", examined]
            cells += [f"{r['p50_ms']:.1f}", f"{r['p95_ms']:.1f}", str(r["leaks"])]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


# ---- main ------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", choices=["fiqa", "synthetic"], default="synthetic")
    p.add_argument("--embedder", default="hashing", help="'hashing' or a sentence-transformers model name")
    p.add_argument("--docs", type=int, default=0, help="limit the corpus (0: all; synthetic default 3000)")
    p.add_argument("--queries", type=int, default=200)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--teams", type=int, default=1000, help="number of teams, a multiple of 100")
    p.add_argument("--selectivity", default="0.001,0.01,0.05,0.2,1")
    p.add_argument("--layouts", default=",".join(LAYOUTS))
    p.add_argument("--backends", default="exact,chroma,pgvector")
    p.add_argument("--database-url", default=os.environ.get("NEEDTOKNOW_BENCH_DATABASE_URL"))
    p.add_argument("--max-candidates", type=int, default=1000)
    p.add_argument("--cell-seconds", type=float, default=300.0, help="time limit for one table row")
    p.add_argument("--batch-size", type=int, default=64, help="embedding batch size")
    p.add_argument("--load-batch", type=int, default=2000, help="chunks per write to Chroma and PostgreSQL")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cache", type=Path, help="directory for cached embeddings")
    p.add_argument("--out", type=Path, default=Path("results.json"))
    p.add_argument("--markdown", type=Path)
    p.add_argument("--check", action="store_true", help="exit 1 on leaks, errors or inexact exact results")
    return p.parse_args(argv)


def embed_corpus(args: argparse.Namespace, docs: list[str], queries: list[str], rng: np.random.Generator) -> Any:
    if args.embedder == "hashing":
        return hashing_embed(docs, rng), hashing_embed(queries, rng)
    prefix = getattr(args, "query_prefix", "")
    cache = None
    if args.cache:
        tag = hashlib.blake2b(prefix.encode(), digest_size=4).hexdigest()
        slug = re.sub(r"[^\w.-]+", "_", f"{args.dataset}-{args.embedder}-{len(docs)}-{len(queries)}-{args.seed}-{tag}")
        cache = args.cache / f"{slug}.npz"
        if cache.exists():
            saved = np.load(cache)
            return saved["docs"], saved["queries"]
    doc_vecs, query_vecs = model_embed(args.embedder, docs, queries, args.batch_size, prefix)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, docs=doc_vecs, queries=query_vecs)
    return doc_vecs, query_vecs


def environment(args: argparse.Namespace) -> dict[str, Any]:
    env: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "needtoknow": needtoknow.__version__,
        "numpy": np.__version__,
    }
    if args.embedder != "hashing":
        import sentence_transformers
        import torch

        env["sentence_transformers"] = sentence_transformers.__version__
        env["torch"] = torch.__version__
        env["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    return env


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    wanted = [b.strip() for b in args.backends.split(",") if b.strip()]
    layouts = [x.strip() for x in args.layouts.split(",") if x.strip()]
    selectivities = [float(s) for s in args.selectivity.split(",")]
    if args.teams % 100 or not all(0 < s <= 1 for s in selectivities) or not set(layouts) <= set(LAYOUTS):
        raise SystemExit("--teams must be a multiple of 100, selectivities in (0, 1], layouts from " + str(LAYOUTS))
    if not set(wanted) <= {"exact", "chroma", "pgvector", "pgvector-rls"}:
        raise SystemExit("--backends takes exact, chroma, pgvector and pgvector-rls")
    if {"pgvector", "pgvector-rls"} & set(wanted) and not args.database_url:
        raise SystemExit("pgvector needs --database-url or NEEDTOKNOW_BENCH_DATABASE_URL")
    rng = np.random.default_rng(args.seed)
    began = time.perf_counter()

    if args.dataset == "fiqa":
        ids, texts, queries = load_fiqa(args.docs, args.queries, rng)
    else:
        ids, texts, queries = load_synthetic(args.docs or 3000, args.queries, rng)
    doc_vecs, query_vecs = embed_corpus(args, texts, queries, rng)
    log(f"{len(ids)} chunks, {len(queries)} queries, dim {doc_vecs.shape[1]} ({time.perf_counter() - began:.0f} s)")

    random_teams = rng.integers(0, args.teams, len(ids))
    anchors = doc_vecs[rng.choice(len(ids), args.teams, replace=False)]
    topical_teams = nearest(doc_vecs, anchors)
    metadata = [
        {FIELD: tags("r", int(a)) + tags("t", int(b))} for a, b in zip(random_teams, topical_teams, strict=True)
    ]
    lookup = dict(zip(queries, query_vecs, strict=True))
    embed = lookup.__getitem__
    row_of = {doc_id: row for row, doc_id in enumerate(ids)}

    env = environment(args)
    backends: dict[str, Any] = {}
    if "exact" in wanted:
        backends["exact"] = ExactBackend(ids, doc_vecs, metadata, embed)
    docs = documents(ids, texts, metadata) if {"chroma", "pgvector", "pgvector-rls"} & set(wanted) else []
    if "chroma" in wanted:
        backends["chroma"], versions = chroma_backend(docs, doc_vecs, embed, args.load_batch)
        env.update(versions)
    if "pgvector" in wanted:
        more, versions = pgvector_backends(args.database_url, docs, doc_vecs, embed, args.load_batch)
        backends.update(more)
        env.update(versions)
    baselines: list[tuple[str, str, Run]] = []
    if "pgvector-rls" in wanted:
        rls, versions = pgvector_rls_run(args.database_url, docs, doc_vecs, embed, args.load_batch)
        baselines.append(("pgvector", RLS, rls))
        env.update(versions)
    ready = [*backends, *(f"{name} {strategy}" for name, strategy, _ in baselines)]
    log(f"backends ready: {', '.join(ready)} ({time.perf_counter() - began:.0f} s)")

    rows = []
    under_test = systems(backends, args.max_candidates) + baselines
    for layout in layouts:
        teams, prefix = (random_teams, "r") if layout == "random" else (topical_teams, "t")
        for selectivity in selectivities:
            cases = []
            for qi, (query, qvec) in enumerate(zip(queries, query_vecs, strict=True)):
                sims = None if layout == "random" else anchors @ qvec
                unit, chosen = subject_units(layout, args.teams, selectivity, sims, rng)
                groups = [f"{prefix}-{LEVELS[unit]}-{int(u)}" for u in chosen]
                permitted = np.isin(teams // unit, chosen)
                scores = np.where(permitted, doc_vecs @ qvec, -np.inf)
                truth = frozenset(ids[r] for r in top_rows(scores, args.k))
                cases.append(Case(query, Subject(f"user-{qi}", groups), permitted, truth))
            readable = float(np.mean([c.permitted.mean() for c in cases]))
            for backend_name, strategy, run in under_test:
                row = {"layout": layout, "selectivity": selectivity, "readable": round(readable, 5)}
                row |= {"backend": backend_name, "strategy": strategy}
                row |= measure(run, cases, args.k, row_of, args.cell_seconds)
                rows.append(row)
                log(
                    f"{layout:>10} {selectivity:>6.1%} {backend_name:>28} {strategy:>11}: recall {row['recall']:.3f}"
                    f" complete {row['complete_rate']:.0%} p50 {row['p50_ms']:.1f} ms leaks {row['leaks']}"
                    f" errors {row['errors']} (n={row['queries']})"
                )

    config = {k: v for k, v in vars(args).items() if k != "database_url"}
    config |= {"docs": len(ids), "queries": len(queries), "dim": int(doc_vecs.shape[1])}
    report = {
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in config.items()},
        "environment": env,
        "seconds": round(time.perf_counter() - began, 1),
        "rows": rows,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(rows, args.k), encoding="utf-8")
    log(f"wrote {args.out} ({report['seconds']} s)")
    if args.check:
        found = problems(rows)
        for problem in found:
            log(f"PROBLEM {problem}")
        if found:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
