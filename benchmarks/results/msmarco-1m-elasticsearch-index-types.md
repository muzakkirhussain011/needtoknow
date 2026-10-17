# MS MARCO, a million chunks, bge-small-en-v1.5, Elasticsearch index types: load benchmark results

Run on 2026-10-17 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) and its
`elasticsearch-index-types` plan, at commit `07ced7a` (Kaggle version 3 of the notebook, about an
hour in total). Elasticsearch ran twice, one index type after the other, in one session on one
machine, and was stopped with its data deleted between the runs. The embeddings, questions and
permissions are the same as in the [run on every store](msmarco-1m-every-store.md), which measured
Elasticsearch's default index.

Two settings changed together from that run: the index type, and the candidates each shard
considers (`num_candidates`), 500 here against the backend's default of 100. The recall gains below
come from both. The default run was a different session on a different machine, so compare its
throughput with these only loosely.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4, `num_candidates` = 500 |
| Store | Elasticsearch 9.5.5, release build, loaded through `ElasticsearchBackend`; `create_index(..., index_options={"type": "int8_hnsw"})`, then `{"type": "hnsw"}` |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, no GPU, shared by the store and the benchmark's client threads |
| Software | needtoknow at `07ced7a`, on Kaggle's Python image |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

## Summary

Pre-filtered recall@10 and median latency, one query at a time:

| Readable | `bbq_hnsw`, 100 candidates (default) | `int8_hnsw`, 500 candidates | `hnsw`, 500 candidates |
|---|---|---|---|
| 0.1% | 0.935, 31.8 ms | 0.988, 39.8 ms | 1.000, 160.8 ms |
| 1% | 0.615, 13.0 ms | 0.983, 10.9 ms | 1.000, 10.9 ms |
| 10% | 0.820, 10.4 ms | 0.932, 12.6 ms | 0.933, 12.5 ms |
| 100% | 0.874, 9.5 ms | 0.966, 9.5 ms | 0.988, 10.2 ms |

| Index | Size | Loading through `add` | Peak throughput (users, p99) | At 16 users (p99) |
|---|---|---|---|---|
| `bbq_hnsw` (default) | 2.2 GiB | 25.2 min | 219 requests/s (4, 34 ms) | 158 requests/s (1,074 ms) |
| `int8_hnsw` | 2.2 GiB | 19.7 min | 246 requests/s (8, 60 ms) | 244 requests/s (87 ms) |
| `hnsw` | 2.5 GiB | 20.9 min | 235 requests/s (16, 89 ms) | 235 requests/s (89 ms) |

No retrieval leaked, pre- or post-filtered, and no request failed under load.

## int8_hnsw

### Load: 1000000 chunks in elasticsearch

Loaded in 1180.1 s, then 0.2 s until the index was built. 1000000 documents, 2.2 GiB. Search breadth: 500. Index type: int8_hnsw.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.988 | 100% | 39.78 | 142.16 | 0 |
| 0.1% | postfilter | 0.094 | 0% | 141.92 | 318.72 | 0 |
| 1.0% | prefilter | 0.983 | 100% | 10.87 | 14.31 | 0 |
| 1.0% | postfilter | 0.831 | 56% | 124.88 | 133.27 | 0 |
| 10.0% | prefilter | 0.932 | 100% | 12.6 | 33.91 | 0 |
| 10.0% | postfilter | 0.959 | 100% | 25.25 | 38.83 | 0 |
| 100.0% | prefilter | 0.966 | 100% | 9.47 | 14.66 | 0 |
| 100.0% | postfilter | 0.966 | 100% | 9.37 | 11.52 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 115.9 | 8.39 | 9.95 | 11.55 | 0 |
| 1.0% | 2 | 158.5 | 11.95 | 17.05 | 20.9 | 0 |
| 1.0% | 4 | 217.6 | 17.65 | 26.4 | 32.19 | 0 |
| 1.0% | 8 | 245.5 | 31.32 | 49.68 | 59.55 | 0 |
| 1.0% | 16 | 244.2 | 39.92 | 65.93 | 86.55 | 0 |

## hnsw

### Load: 1000000 chunks in elasticsearch

Loaded in 1251.3 s, then 0.1 s until the index was built. 1000000 documents, 2.5 GiB. Search breadth: 500. Index type: hnsw.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 1.000 | 100% | 160.81 | 296.83 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 144.01 | 383.04 | 0 |
| 1.0% | prefilter | 1.000 | 100% | 10.87 | 14.56 | 0 |
| 1.0% | postfilter | 0.834 | 56% | 127.84 | 138.32 | 0 |
| 10.0% | prefilter | 0.933 | 100% | 12.52 | 28.23 | 0 |
| 10.0% | postfilter | 0.973 | 100% | 26.63 | 63.13 | 0 |
| 100.0% | prefilter | 0.988 | 100% | 10.17 | 15.38 | 0 |
| 100.0% | postfilter | 0.988 | 100% | 9.6 | 11.21 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 107.1 | 9.08 | 10.55 | 13.79 | 0 |
| 1.0% | 2 | 153.0 | 12.55 | 17.72 | 21.67 | 0 |
| 1.0% | 4 | 205.2 | 18.68 | 27.43 | 34.28 | 0 |
| 1.0% | 8 | 229.9 | 33.79 | 52.01 | 61.38 | 0 |
| 1.0% | 16 | 234.6 | 41.5 | 67.64 | 88.85 | 0 |
