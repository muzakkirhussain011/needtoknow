# MS MARCO, a million chunks, bge-small-en-v1.5, Qdrant: load benchmark results

Run on 2026-10-17 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) (`STORE = "qdrant"`) at
commit `1d8348e` (Kaggle version 1 of the notebook, 1,643 s in total). The embeddings are the ones
the [pgvector run](msmarco-1m-bge-small.md) computed, attached as input, so both stores were
measured on the same million vectors, queries and permissions.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Store | Qdrant 1.19.2 from its release build; collection made by `create_collection` (cosine, keyword payload indexes on the permission fields), loaded through `QdrantBackend`, at Qdrant's default HNSW and search settings |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, shared by Qdrant and the benchmark's client threads |
| Software | Python 3.13.15, needtoknow at `1d8348e`, NumPy 2.1.3 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

### Load: 1000000 chunks in qdrant

Loaded in 826.5 s, then 358.7 s until the index was built. 1000000 points, 1000000 indexed vectors. Search breadth: the store default.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 3.4 | 5.01 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 93.46 | 110.11 | 0 |
| 1.0% | prefilter | 0.994 | 100% | 5.06 | 7.87 | 0 |
| 1.0% | postfilter | 0.857 | 56% | 85.81 | 91.97 | 0 |
| 10.0% | prefilter | 0.980 | 100% | 5.03 | 6.13 | 0 |
| 10.0% | postfilter | 0.959 | 100% | 14.73 | 18.72 | 0 |
| 100.0% | prefilter | 0.972 | 100% | 5.82 | 6.95 | 0 |
| 100.0% | postfilter | 0.972 | 100% | 5.32 | 5.94 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 202.1 | 4.89 | 5.7 | 6.4 | 0 |
| 1.0% | 2 | 275.6 | 7.18 | 9.08 | 9.92 | 0 |
| 1.0% | 4 | 304.3 | 12.89 | 18.33 | 20.53 | 0 |
| 1.0% | 8 | 312.5 | 24.84 | 37.99 | 45.14 | 0 |
| 1.0% | 16 | 309.6 | 51.25 | 65.77 | 75.12 | 0 |
