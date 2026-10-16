"""Load benchmark: permission-aware retrieval at a million chunks, with concurrent users.

On PostgreSQL + pgvector it measures

* how long loading and indexing the corpus takes, and how large it gets,
* recall@k against brute force over the same vectors, and latency, by permission selectivity, one
  query at a time,
* throughput and latency percentiles as the number of concurrent users grows,
* what a remote permission service costs per retrieval: the in-process MetadataACLAuthorizer
  against Open Policy Agent over HTTP, with and without CachingAuthorizer (`--opa-url`).

Passages are the first `--docs` of the BEIR MS MARCO corpus and queries are real MS MARCO
questions, embedded with an open model on a GPU (see benchmarks/README.md). Permissions follow the
hierarchical team layout of permissions_bench.py. `--dataset synthetic --embedder hashing` runs
anywhere without a model, for CI; `--check` then fails on any leak or error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from permissions_bench import (
    LEVELS,
    Case,
    embed_corpus,
    environment,
    load_synthetic,
    log,
    measure,
    needtoknow_run,
    percentile,
    subject_units,
    tags,
    top_rows,
)

from needtoknow import CachingAuthorizer, MetadataACLAuthorizer, PermissionedRetriever, Subject

TABLE = "needtoknow_load"
FIELD = "allowed_groups"


def load_msmarco(n_docs: int, n_queries: int, rng: np.random.Generator) -> tuple[list[str], list[str], list[str]]:
    from datasets import load_dataset

    ids, texts = [], []
    for row in load_dataset("BeIR/msmarco", "corpus", split="corpus", streaming=True):
        text = f"{row['title']}\n{row['text']}".strip()
        if text:
            ids.append(row["_id"])
            texts.append(text)
            if len(ids) % 100_000 == 0:
                log(f"read {len(ids)} passages")
        if len(ids) >= n_docs:
            break
    questions = load_dataset("BeIR/msmarco", "queries", split="queries")
    unique = list(dict.fromkeys(q.strip() for q in questions["text"] if q.strip()))
    picked = rng.choice(len(unique), min(n_queries, len(unique)), replace=False)
    return ids, texts, [unique[i] for i in picked]


def load_postgres(url: str, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
    """Bulk-load with binary COPY, then build the indexes; returns timings and sizes."""
    import psycopg
    from pgvector.psycopg import register_vector
    from psycopg import sql
    from psycopg.types.json import Jsonb

    table = sql.Identifier(TABLE)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
        conn.execute(
            sql.SQL(
                "CREATE TABLE {} (id text PRIMARY KEY, content text NOT NULL, metadata jsonb NOT NULL,"
                " embedding vector({}) NOT NULL)"
            ).format(table, sql.Literal(int(vectors.shape[1])))
        )
        started = time.perf_counter()
        copy_sql = sql.SQL("COPY {} (id, content, metadata, embedding) FROM STDIN WITH (FORMAT BINARY)")
        with conn.cursor() as cur, cur.copy(copy_sql.format(table)) as copy:
            copy.set_types(["text", "text", "jsonb", "vector"])
            for i in range(len(ids)):
                copy.write_row((ids[i], texts[i], Jsonb(metadata[i]), vectors[i]))
        loaded = time.perf_counter()
        log(f"loaded {len(ids)} rows in {loaded - started:.0f} s")
        conn.execute("SET maintenance_work_mem = '4GB'")
        conn.execute("SET max_parallel_maintenance_workers = 3")
        conn.execute(
            sql.SQL("CREATE INDEX {} ON {} USING gin (metadata jsonb_path_ops)").format(
                sql.Identifier(f"{TABLE}_metadata_gin"), table
            )
        )
        gin = time.perf_counter()
        conn.execute(
            sql.SQL("CREATE INDEX {} ON {} USING hnsw (embedding vector_cosine_ops)").format(
                sql.Identifier(f"{TABLE}_embedding_hnsw"), table
            )
        )
        hnsw = time.perf_counter()
        log(f"indexes built: GIN {gin - loaded:.0f} s, HNSW {hnsw - gin:.0f} s")
        conn.execute(sql.SQL("ANALYZE {}").format(table))
        size = conn.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", [TABLE]).fetchone()[0]
        server = conn.execute("SHOW server_version").fetchone()[0]
        extension = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
    return {
        "rows": len(ids),
        "copy_s": round(loaded - started, 1),
        "gin_index_s": round(gin - loaded, 1),
        "hnsw_index_s": round(hnsw - gin, 1),
        "total_size": size,
        "postgres": server,
        "pgvector": extension,
    }


def backend(url: str, embed: Any, pool_size: int) -> Any:
    from psycopg_pool import ConnectionPool

    from needtoknow.backends.pgvector import PgVectorBackend

    pool = ConnectionPool(url, min_size=pool_size, max_size=pool_size, open=True)
    return PgVectorBackend(pool, embed, table=TABLE)


def throughput(retriever: PermissionedRetriever, cases: Sequence[Case], k: int, users: int, seconds: float) -> dict:
    """`users` threads retrieve in a loop for `seconds`; each request is timed."""
    latencies: list[float] = []
    errors = [0]
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def user(first: int) -> None:
        mine, failed, i = [], 0, first
        while time.perf_counter() < stop:
            case = cases[i % len(cases)]
            i += users
            start = time.perf_counter()
            try:
                retriever.retrieve(case.query, case.subject, k)
            except Exception:
                failed += 1
                continue
            mine.append((time.perf_counter() - start) * 1000)
        with lock:
            latencies.extend(mine)
            errors[0] += failed

    began = time.perf_counter()
    threads = [threading.Thread(target=user, args=(n,)) for n in range(users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - began
    return {
        "users": users,
        "requests": len(latencies),
        "qps": round(len(latencies) / elapsed, 1),
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "errors": errors[0],
    }


def build_cases(
    queries: Sequence[str],
    query_vecs: np.ndarray,
    doc_vecs: np.ndarray,
    teams: np.ndarray,
    ids: Sequence[str],
    n_teams: int,
    selectivity: float,
    k: int,
    rng: np.random.Generator,
) -> list[Case]:
    cases = []
    for qi, (query, qvec) in enumerate(zip(queries, query_vecs, strict=True)):
        unit, chosen = subject_units("random", n_teams, selectivity, None, rng)
        permitted = np.isin(teams // unit, chosen)
        truth = frozenset(ids[r] for r in top_rows(np.where(permitted, doc_vecs @ qvec, -np.inf), k))
        groups = [f"r-{LEVELS[unit]}-{int(u)}" for u in chosen]
        cases.append(Case(query, Subject(f"user-{qi}", groups), permitted, truth))
    return cases


SCALE_COLUMNS = [
    ("Selectivity", "selectivity"),
    ("Strategy", "strategy"),
    ("Recall@k", "recall"),
    ("Complete", "complete_rate"),
    ("p50 ms", "p50_ms"),
    ("p95 ms", "p95_ms"),
    ("Leaks", "leaks"),
]
LOAD_COLUMNS = [
    ("Requests/s", "qps"),
    ("p50 ms", "p50_ms"),
    ("p95 ms", "p95_ms"),
    ("p99 ms", "p99_ms"),
    ("Errors", "errors"),
]


def markdown(report: dict) -> str:
    def table(rows: list[dict], columns: list[tuple[str, str]]) -> list[str]:
        out = ["| " + " | ".join(title for title, _ in columns) + " |", "|---" * len(columns) + "|"]
        for r in rows:
            out.append("| " + " | ".join(str(r[key]) for _, key in columns) + " |")
        return [*out, ""]

    load = report["load"]
    out = [
        f"### Load: {load['rows']} chunks",
        "",
        f"COPY {load['copy_s']} s, GIN index {load['gin_index_s']} s, HNSW index {load['hnsw_index_s']} s,"
        f" total size {load['total_size']}.",
        "",
        "### Recall and latency by selectivity (one query at a time)",
        "",
    ]
    out += table(report["scale"], SCALE_COLUMNS)
    out += ["### Throughput with concurrent users", ""]
    out += table(report["throughput"], [("Selectivity", "selectivity"), ("Users", "users"), *LOAD_COLUMNS])
    if report["authorizers"]:
        out += ["### Cost of the permission service", ""]
        out += table(report["authorizers"], [("Authorizer", "authorizer"), ("Users", "users"), *LOAD_COLUMNS])
    return "\n".join(out)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", choices=["msmarco", "synthetic"], default="synthetic")
    p.add_argument("--embedder", default="hashing", help="'hashing' or a sentence-transformers model name")
    p.add_argument("--query-prefix", default="", help="prepended to queries, for models that expect one")
    p.add_argument("--docs", type=int, default=0, help="corpus size (0: 1,000,000; synthetic 20,000)")
    p.add_argument("--queries", type=int, default=300)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--teams", type=int, default=1000, help="a multiple of 100")
    p.add_argument("--selectivity", default="0.001,0.01,0.1,1")
    p.add_argument("--throughput-selectivity", type=float, default=0.01)
    p.add_argument("--users", default="1,2,4,8,16", help="concurrent users for the throughput runs")
    p.add_argument("--seconds", type=float, default=20.0, help="duration of each throughput run")
    p.add_argument("--database-url", default=os.environ.get("NEEDTOKNOW_BENCH_DATABASE_URL"))
    p.add_argument("--opa-url", help="an OPA running examples/opa, to measure a remote permission service")
    p.add_argument("--batch-size", type=int, default=256, help="embedding batch size")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cache", type=Path, help="directory for cached embeddings")
    p.add_argument("--out", type=Path, default=Path("load_results.json"))
    p.add_argument("--markdown", type=Path)
    p.add_argument("--check", action="store_true", help="exit 1 on any leak or error")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.database_url:
        raise SystemExit("needs --database-url or NEEDTOKNOW_BENCH_DATABASE_URL")
    if args.teams % 100:
        raise SystemExit("--teams must be a multiple of 100")
    selectivities = [float(s) for s in args.selectivity.split(",")]
    users = [int(u) for u in args.users.split(",")]
    rng = np.random.default_rng(args.seed)
    began = time.perf_counter()

    if args.dataset == "msmarco":
        ids, texts, queries = load_msmarco(args.docs or 1_000_000, args.queries, rng)
    else:
        ids, texts, queries = load_synthetic(args.docs or 20_000, args.queries, rng)
    doc_vecs, query_vecs = embed_corpus(args, texts, queries, rng)
    log(f"{len(ids)} chunks, {len(queries)} queries, dim {doc_vecs.shape[1]} ({time.perf_counter() - began:.0f} s)")

    teams = rng.integers(0, args.teams, len(ids))
    metadata = [{FIELD: tags("r", int(t))} for t in teams]
    load = load_postgres(args.database_url, ids, texts, doc_vecs, metadata)
    del texts
    embed: Callable[[str], Any] = dict(zip(queries, query_vecs, strict=True)).__getitem__
    row_of = {doc_id: row for row, doc_id in enumerate(ids)}
    acl = MetadataACLAuthorizer()

    scale = []
    single = backend(args.database_url, embed, 1)
    for selectivity in selectivities:
        cases = build_cases(queries, query_vecs, doc_vecs, teams, ids, args.teams, selectivity, args.k, rng)
        for strategy in ("prefilter", "postfilter"):
            row = {"selectivity": f"{selectivity:.1%}", "strategy": strategy}
            row |= measure(needtoknow_run(single, acl, strategy, 1000), cases, args.k, row_of, 600)
            scale.append(row)
            log(
                f"scale {row['selectivity']:>6} {strategy:>10}: recall {row['recall']:.3f}"
                f" p50 {row['p50_ms']} ms leaks {row['leaks']} errors {row['errors']}"
            )

    busy = build_cases(queries, query_vecs, doc_vecs, teams, ids, args.teams, args.throughput_selectivity, args.k, rng)
    del doc_vecs
    rows = []
    for n in users:
        retriever = PermissionedRetriever(backend(args.database_url, embed, n), acl, strategy="prefilter")
        row = {"selectivity": f"{args.throughput_selectivity:.1%}"}
        row |= throughput(retriever, busy, args.k, n, args.seconds)
        rows.append(row)
        log(
            f"throughput {n:>3} users: {row['qps']} req/s, p50 {row['p50_ms']} ms, p99 {row['p99_ms']} ms,"
            f" errors {row['errors']}"
        )

    authorizers = []
    if args.opa_url:
        from needtoknow.authorizers.opa import OPAAuthorizer

        def opa() -> OPAAuthorizer:
            return OPAAuthorizer(args.opa_url, filter_path="needtoknow/filter")

        candidates = [
            ("in-process (MetadataACLAuthorizer)", lambda: acl),
            ("Open Policy Agent over HTTP", opa),
            ("Open Policy Agent, CachingAuthorizer(ttl=60)", lambda: CachingAuthorizer(opa(), ttl=60)),
        ]
        for name, make in candidates:
            for n in sorted({1, max(users)}):
                retriever = PermissionedRetriever(backend(args.database_url, embed, n), make(), strategy="prefilter")
                row = {"authorizer": name} | throughput(retriever, busy, args.k, n, args.seconds)
                authorizers.append(row)
                log(f"{name}, {n} users: {row['qps']} req/s, p50 {row['p50_ms']} ms, errors {row['errors']}")

    config = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items() if k != "database_url"}
    report = {
        "config": config | {"docs": len(ids), "queries": len(queries), "dim": int(query_vecs.shape[1])},
        "environment": environment(args),
        "seconds": round(time.perf_counter() - began, 1),
        "load": load,
        "scale": scale,
        "throughput": rows,
        "authorizers": authorizers,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(markdown(report), encoding="utf-8")
    log(f"wrote {args.out} ({report['seconds']} s)")
    if args.check:
        problems = [f"{r['selectivity']} {r['strategy']}: {r['leaks']} leaks" for r in scale if r["leaks"]]
        problems += [f"errors in {r}" for r in scale + rows + authorizers if r["errors"]]
        for problem in problems:
            log(f"PROBLEM {problem}")
        if problems or not any(r["requests"] for r in rows):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
