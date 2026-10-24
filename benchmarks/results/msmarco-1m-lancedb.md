# MS MARCO, a million chunks, bge-small-en-v1.5, LanceDB: load benchmark results

Run on 2026-10-17 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) and its `lancedb` plan, at
commit `0f4a182` (Kaggle version 4 of the notebook, 610 s for the benchmark itself). The
embeddings, questions and permissions are the same as in the [run on every store](msmarco-1m-every-store.md).
LanceDB is embedded, so it ran inside the benchmark's own process, on a different machine and in a
different session from the other stores: compare its numbers with theirs only loosely.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Store | LanceDB (the `lancedb` extra, 0.40 or later), embedded, on local disk. The backend's row layout bulk-loaded with `table.add` in batches of 100,000, then `create_indexes` (the `LABEL_LIST` index on the permission terms) and LanceDB's default vector index, IVF_PQ with cosine distance; `nprobes` and `refine_factor` at their defaults |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, no GPU, shared by LanceDB and the benchmark's client threads |
| Software | wardowl at `0f4a182`, on Kaggle's Python image |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

## Summary

- **No retrieval leaked**, pre- or post-filtered, and no request failed under load.
- **Recall is about half at LanceDB's defaults, at every share readable** (0.47 to 0.51
  pre-filtered). The default index quantizes vectors (product quantization) and, without
  `refine_factor`, ranks by the quantized distances. Pre-filtering itself costs nothing here:
  recall at 0.1% readable is the same as with no filter. `LanceDBBackend(..., refine_factor=...)`
  re-ranks with the full vectors and `nprobes=` searches more partitions; both trade latency for
  recall and are not measured here yet.
- **Loading is fast in bulk**: 21 s for the million chunks with `table.add`, then 75 s for the
  indexes.
- **Throughput peaked at 140 requests a second** with 16 users (p99 168 ms), with LanceDB running
  inside the client process.

## Load: 1000000 chunks in lancedb

Loaded in 20.8 s, then 74.5 s until the index was built. Bulk-loaded with table.add; LanceDB's default vector index (IVF_PQ), cosine. Search breadth: the store default.

## Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.503 | 100% | 15.83 | 18.58 | 0 |
| 0.1% | postfilter | 0.080 | 0% | 93.42 | 99.45 | 0 |
| 1.0% | prefilter | 0.471 | 100% | 17.36 | 19.18 | 0 |
| 1.0% | postfilter | 0.436 | 50% | 93.77 | 100.58 | 0 |
| 10.0% | prefilter | 0.466 | 100% | 19.53 | 21.56 | 0 |
| 10.0% | postfilter | 0.466 | 100% | 31.93 | 40.94 | 0 |
| 100.0% | prefilter | 0.507 | 100% | 28.66 | 31.35 | 0 |
| 100.0% | postfilter | 0.507 | 100% | 13.55 | 15.09 | 0 |

## Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 55.3 | 17.95 | 19.53 | 21.46 | 0 |
| 1.0% | 2 | 86.6 | 22.71 | 27.26 | 29.09 | 0 |
| 1.0% | 4 | 125.9 | 31.32 | 38.18 | 42.12 | 0 |
| 1.0% | 8 | 137.6 | 57.31 | 72.38 | 80.43 | 0 |
| 1.0% | 16 | 140.0 | 113.02 | 146.24 | 168.2 | 0 |
