# MS MARCO, a million chunks, bge-small-en-v1.5: load benchmark results

Run on 2026-10-22 with [`kaggle_load.ipynb`](../kaggle_load.ipynb) at commit `910f16b`
(Kaggle version 4 of the notebook, 1,636 s in total, of which about 930 s reading and embedding).
The run before it, on 2026-10-16 at `31b8dbc`, did not yet check results under load; its recall
and request rates were within 4% of these.

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
| Software | Python 3.13.15, needtoknow at `910f16b`, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0, PostgreSQL 16.15, pgvector 0.8.7, OPA 1.21.2 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the number of returned chunks the subject may not
read: over 300 queries of 10 results one at a time, and under load over every chunk returned, each
checked against the permissions of the subject it was returned to (*Chunks checked*).

### Load: 1000000 chunks

COPY 35.9 s, GIN index 3.0 s, HNSW index 263.0 s, total size 4071 MB. hnsw.ef_search: default (40).

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.968 | 100% | 10.61 | 78.69 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 65.52 | 83.24 | 0 |
| 1.0% | prefilter | 0.927 | 100% | 21.37 | 41.62 | 0 |
| 1.0% | postfilter | 0.831 | 55% | 59.77 | 77.76 | 0 |
| 10.0% | prefilter | 0.883 | 100% | 4.35 | 6.47 | 0 |
| 10.0% | postfilter | 0.916 | 100% | 9.05 | 15.1 | 0 |
| 100.0% | prefilter | 0.890 | 100% | 3.28 | 4.71 | 0 |
| 100.0% | postfilter | 0.890 | 100% | 2.73 | 3.47 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|---|
| 1.0% | 1 | 42.9 | 22.18 | 42.38 | 44.38 | 0 | 8580 | 0 |
| 1.0% | 2 | 81.1 | 23.45 | 44.5 | 50.43 | 0 | 16250 | 0 |
| 1.0% | 4 | 125.9 | 29.63 | 62.76 | 69.62 | 0 | 25220 | 0 |
| 1.0% | 8 | 118.6 | 61.84 | 131.61 | 179.55 | 0 | 23770 | 0 |
| 1.0% | 16 | 109.5 | 131.35 | 273.96 | 438.98 | 0 | 22000 | 0 |

### Cost of the permission service

| Authorizer | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|---|
| in-process (MetadataACLAuthorizer) | 1 | 42.0 | 22.13 | 42.77 | 46.17 | 0 | 8410 | 0 |
| in-process (MetadataACLAuthorizer) | 16 | 111.1 | 131.04 | 270.49 | 405.48 | 0 | 22310 | 0 |
| Open Policy Agent over HTTP | 1 | 38.6 | 24.33 | 44.38 | 47.06 | 0 | 7720 | 0 |
| Open Policy Agent over HTTP | 16 | 101.2 | 144.24 | 297.92 | 431.85 | 0 | 20320 | 0 |
| Open Policy Agent, CachingAuthorizer(ttl=60) | 1 | 40.6 | 22.94 | 42.54 | 47.02 | 0 | 8120 | 0 |
| Open Policy Agent, CachingAuthorizer(ttl=60) | 16 | 111.2 | 130.02 | 272.24 | 423.79 | 0 | 22340 | 0 |
