# MS MARCO, a million chunks, bge-small-en-v1.5: load benchmark results

Run on 2026-10-16 with [`kaggle_load.ipynb`](../kaggle_load.ipynb) at commit `31b8dbc`
(Kaggle version 3 of the notebook, 1,837 s in total, of which about 1,000 s embedding).

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions, fp16, maximum 512 tokens; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Indexes | GIN (`jsonb_path_ops`) on the permission metadata; HNSW (cosine) at default settings: m 16, ef_construction 64, ef_search 40; iterative scans `relaxed_order` |
| PostgreSQL | shared_buffers 4 GB, effective_cache_size 16 GB; indexes built with maintenance_work_mem 4 GB and 3 parallel workers |
| Load | Throughput and permission-service runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, shared by PostgreSQL, OPA and the benchmark's client threads; Tesla T4 for the embeddings only |
| Software | Python 3.13.15, needtoknow at `31b8dbc`, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0, PostgreSQL 16.15, pgvector 0.8.7, OPA 1.21.2 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

### Load: 1000000 chunks

COPY 36.0 s, GIN index 5.6 s, HNSW index 265.4 s, total size 4071 MB.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.972 | 100% | 10.99 | 84.1 | 0 |
| 0.1% | postfilter | 0.094 | 0% | 62.61 | 79.94 | 0 |
| 1.0% | prefilter | 0.933 | 100% | 21.18 | 42.86 | 0 |
| 1.0% | postfilter | 0.833 | 55% | 59.38 | 76.12 | 0 |
| 10.0% | prefilter | 0.883 | 100% | 4.27 | 6.4 | 0 |
| 10.0% | postfilter | 0.915 | 100% | 8.95 | 14.09 | 0 |
| 100.0% | prefilter | 0.895 | 100% | 3.17 | 4.78 | 0 |
| 100.0% | postfilter | 0.895 | 100% | 2.76 | 3.52 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 44.5 | 21.54 | 41.19 | 44.79 | 0 |
| 1.0% | 2 | 82.7 | 22.98 | 43.69 | 49.57 | 0 |
| 1.0% | 4 | 129.2 | 28.85 | 61.82 | 72.07 | 0 |
| 1.0% | 8 | 121.9 | 60.18 | 127.03 | 173.06 | 0 |
| 1.0% | 16 | 112.1 | 128.16 | 266.61 | 430.8 | 0 |

### Cost of the permission service

| Authorizer | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| in-process (MetadataACLAuthorizer) | 1 | 43.5 | 21.42 | 41.33 | 45.89 | 0 |
| in-process (MetadataACLAuthorizer) | 16 | 113.4 | 128.05 | 267.84 | 405.43 | 0 |
| Open Policy Agent over HTTP | 1 | 39.1 | 23.91 | 43.69 | 48.19 | 0 |
| Open Policy Agent over HTTP | 16 | 104.8 | 140.31 | 286.27 | 397.15 | 0 |
| Open Policy Agent, CachingAuthorizer(ttl=60) | 1 | 41.4 | 22.55 | 41.98 | 46.3 | 0 |
| Open Policy Agent, CachingAuthorizer(ttl=60) | 16 | 114.2 | 127.06 | 265.59 | 401.71 | 0 |
