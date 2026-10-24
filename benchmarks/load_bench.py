"""Load benchmark: permission-aware retrieval at a million chunks, with concurrent users.

On one vector store (`--backend`: pgvector by default, or Qdrant, Milvus, Elasticsearch,
OpenSearch, Weaviate; see stores.py) it measures

* how long loading and indexing the corpus takes, and how large it gets,
* recall@k against brute force over the same vectors, and latency, by permission selectivity, one
  query at a time,
* throughput and latency percentiles as the number of concurrent users grows, with every chunk
  returned under load checked against the permissions of the subject it was returned to, and each
  measurement repeated `--repeat` times to show how much it varies,
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
from functools import partial
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
    wardowl_run,
    percentile,
    subject_units,
    tags,
    top_rows,
)
from stores import STORES, make_store

from wardowl import CachingAuthorizer, MetadataACLAuthorizer, PermissionedRetriever, Subject

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


def throughput(
    retriever: PermissionedRetriever,
    cases: Sequence[Case],
    k: int,
    users: int,
    seconds: float,
    row_of: dict[str, int],
) -> dict:
    """`users` threads retrieve in a loop for `seconds`; each request is timed.

    The threads take turns through the cases, so requests for different subjects run at the same
    time, and every chunk each one returns is checked against that request's own subject, after the
    request is timed. A chunk returned to the wrong subject under load counts as a leak.
    """
    latencies: list[float] = []
    errors, leaks, checked = [0], [0], [0]
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def user(first: int) -> None:
        mine, failed, bad, seen, i = [], 0, 0, 0, first
        while time.perf_counter() < stop:
            case = cases[i % len(cases)]
            i += users
            start = time.perf_counter()
            try:
                hits = retriever.retrieve(case.query, case.subject, k).hits
            except Exception:
                failed += 1
                continue
            mine.append((time.perf_counter() - start) * 1000)
            seen += len(hits)
            bad += sum(1 for h in hits if not case.permitted[row_of[h.id]])
        with lock:
            latencies.extend(mine)
            errors[0] += failed
            leaks[0] += bad
            checked[0] += seen

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
        "checked": checked[0],
        "leaks": leaks[0],
    }


def repeated(measure_once: Callable[[], dict], times: int) -> dict:
    """Run one throughput measurement `times` times, as `--repeat` asks.

    The row reports the median of each rate and latency, with the slowest and fastest request rates,
    and the sums of requests, errors, chunks checked and leaks, so a leak in any run shows. Every run
    is kept under "runs".
    """
    runs = [measure_once() for _ in range(times)]
    if times == 1:
        return runs[0]
    row: dict[str, Any] = {"users": runs[0]["users"]}
    for key in ("qps", "p50_ms", "p95_ms", "p99_ms"):
        row[key] = round(float(np.median([r[key] for r in runs])), 2)
    row["qps_min"] = min(r["qps"] for r in runs)
    row["qps_max"] = max(r["qps"] for r in runs)
    for key in ("requests", "errors", "checked", "leaks"):
        row[key] = sum(r[key] for r in runs)
    row["runs"] = runs
    return row


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
    ("Chunks checked", "checked"),
    ("Leaks", "leaks"),
]
# As in permissions_bench.py's tables; other values print as they are.
FORMATS = {"recall": "{:.3f}", "complete_rate": "{:.0%}"}


def markdown(report: dict) -> str:
    def cell(row: dict, key: str) -> str:
        if key not in row:  # reports from before results were checked under load have no such column
            return "not checked"
        if key == "qps" and "qps_min" in row:  # --repeat: the median, then the slowest and fastest run
            return f"{row['qps']} ({row['qps_min']} to {row['qps_max']})"
        return FORMATS.get(key, "{}").format(row[key])

    def table(rows: list[dict], columns: list[tuple[str, str]]) -> list[str]:
        out = ["| " + " | ".join(title for title, _ in columns) + " |", "|---" * len(columns) + "|"]
        for r in rows:
            out.append("| " + " | ".join(cell(r, key) for _, key in columns) + " |")
        return [*out, ""]

    load, store = report["load"], report["config"].get("backend", "pgvector")
    if "copy_s" in load:  # pgvector, as in the runs published before other stores were added
        summary = (
            f"COPY {load['copy_s']} s, GIN index {load['gin_index_s']} s, HNSW index {load['hnsw_index_s']} s,"
            f" total size {load['total_size']}. hnsw.ef_search: {report['config'].get('ef_search') or 'default (40)'}."
        )
    else:
        summary = (
            f"Loaded in {load['load_s']} s, then {load['index_s']} s until the index was built. {load['detail']}."
            f" Search breadth: {report['config'].get('ef_search') or 'the store default'}."
        )
        if report["config"].get("index_type"):
            summary += f" Index type: {report['config']['index_type']}."
        if report["config"].get("refine_factor"):
            summary += f" Refine factor: {report['config']['refine_factor']}."
    out = [
        f"### Load: {load['rows']} chunks" + ("" if store == "pgvector" else f" in {store}"),
        "",
        summary,
        "",
        "### Recall and latency by selectivity (one query at a time)",
        "",
    ]
    repeat = report["config"].get("repeat", 1)
    note: list[str] = []
    if repeat > 1:
        note = [
            f"Each row is the median of {repeat} runs of {report['config']['seconds']} s; requests/s also gives"
            " the slowest and fastest run. Errors, chunks checked and leaks are totals over the runs.",
            "",
        ]
    out += table(report["scale"], SCALE_COLUMNS)
    out += ["### Throughput with concurrent users", "", *note]
    out += table(report["throughput"], [("Selectivity", "selectivity"), ("Users", "users"), *LOAD_COLUMNS])
    if report["authorizers"]:
        out += ["### Cost of the permission service", "", *note]
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
    p.add_argument("--repeat", type=int, default=1, help="runs per throughput row; the row reports their median")
    p.add_argument("--backend", choices=STORES, default="pgvector", help="the vector store to measure")
    p.add_argument("--database-url", default=os.environ.get("WARDOWL_BENCH_DATABASE_URL"), help="for pgvector")
    p.add_argument("--url", default=os.environ.get("WARDOWL_BENCH_URL"), help="for the other stores")
    p.add_argument("--grpc-port", type=int, default=50051, help="Weaviate's gRPC port")
    p.add_argument("--ef-search", type=int, help="search breadth (pgvector hnsw.ef_search; see stores.py for the rest)")
    p.add_argument("--index-type", help="Elasticsearch's vector index type, such as int8_hnsw or hnsw")
    p.add_argument("--refine-factor", type=int, help="LanceDB's refine_factor (re-ranks with the full vectors)")
    p.add_argument("--opa-url", help="an OPA running examples/opa, to measure a remote permission service")
    p.add_argument("--batch-size", type=int, default=256, help="embedding batch size")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cache", type=Path, help="directory for cached embeddings")
    p.add_argument("--out", type=Path, default=Path("load_results.json"))
    p.add_argument("--markdown", type=Path)
    p.add_argument("--check", action="store_true", help="exit 1 on any leak or error")
    p.add_argument("--render", type=Path, help="write the tables of an earlier --out file, then exit")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.render:
        text = markdown(json.loads(args.render.read_text(encoding="utf-8")))
        if args.markdown:
            args.markdown.write_text(text, encoding="utf-8")
        else:
            print(text)
        return 0
    url = args.database_url if args.backend == "pgvector" else args.url
    if not url:
        raise SystemExit("pgvector needs --database-url (or WARDOWL_BENCH_DATABASE_URL); other stores --url")
    if args.teams % 100:
        raise SystemExit("--teams must be a multiple of 100")
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
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
    store = make_store(args.backend, url, args.ef_search, args.grpc_port, args.index_type, args.refine_factor)
    load = store.load(ids, texts, doc_vecs, metadata)
    del texts
    embed: Callable[[str], Any] = dict(zip(queries, query_vecs, strict=True)).__getitem__
    row_of = {doc_id: row for row, doc_id in enumerate(ids)}
    acl = MetadataACLAuthorizer()

    scale = []
    single = store.backend(embed, 1)
    for selectivity in selectivities:
        cases = build_cases(queries, query_vecs, doc_vecs, teams, ids, args.teams, selectivity, args.k, rng)
        for strategy in ("prefilter", "postfilter"):
            row = {"selectivity": f"{selectivity:.1%}", "strategy": strategy}
            row |= measure(wardowl_run(single, acl, strategy, 1000), cases, args.k, row_of, 600)
            scale.append(row)
            log(
                f"scale {row['selectivity']:>6} {strategy:>10}: recall {row['recall']:.3f}"
                f" p50 {row['p50_ms']} ms leaks {row['leaks']} errors {row['errors']}"
            )

    busy = build_cases(queries, query_vecs, doc_vecs, teams, ids, args.teams, args.throughput_selectivity, args.k, rng)
    del doc_vecs
    rows = []
    for n in users:
        retriever = PermissionedRetriever(store.backend(embed, n), acl, strategy="prefilter")
        row = {"selectivity": f"{args.throughput_selectivity:.1%}"}
        row |= repeated(partial(throughput, retriever, busy, args.k, n, args.seconds, row_of), args.repeat)
        rows.append(row)
        log(
            f"throughput {n:>3} users: {row['qps']} req/s, p50 {row['p50_ms']} ms, p99 {row['p99_ms']} ms,"
            f" errors {row['errors']}, leaks {row['leaks']} in {row['checked']} chunks checked"
        )

    authorizers = []
    if args.opa_url:
        from wardowl.authorizers.opa import OPAAuthorizer

        def opa() -> OPAAuthorizer:
            return OPAAuthorizer(args.opa_url, filter_path="wardowl/filter")

        candidates = [
            ("in-process (MetadataACLAuthorizer)", lambda: acl),
            ("Open Policy Agent over HTTP", opa),
            ("Open Policy Agent, CachingAuthorizer(ttl=60)", lambda: CachingAuthorizer(opa(), ttl=60)),
        ]
        for name, make in candidates:
            for n in sorted({1, max(users)}):
                retriever = PermissionedRetriever(store.backend(embed, n), make(), strategy="prefilter")
                once = partial(throughput, retriever, busy, args.k, n, args.seconds, row_of)
                row = {"authorizer": name} | repeated(once, args.repeat)
                authorizers.append(row)
                log(
                    f"{name}, {n} users: {row['qps']} req/s, p50 {row['p50_ms']} ms, errors {row['errors']},"
                    f" leaks {row['leaks']}"
                )

    secret = ("database_url", "url")  # connection strings can hold credentials
    config = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items() if k not in secret}
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
        problems += [f"{r['users']} concurrent users: {r['leaks']} leaks" for r in rows + authorizers if r["leaks"]]
        problems += [f"errors in {r}" for r in scale + rows + authorizers if r["errors"]]
        for problem in problems:
            log(f"PROBLEM {problem}")
        # A run that returned nothing under load would check nothing, so it does not pass either.
        if problems or not any(r["requests"] for r in rows) or not any(r["checked"] for r in rows):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
