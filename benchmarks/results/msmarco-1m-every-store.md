# MS MARCO, a million chunks, bge-small-en-v1.5, four stores: load benchmark results

Run on 2026-10-17 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) at commit `5a49eec` (Kaggle
version 2 of the notebook, 9,739 s in total). The four stores ran one after another in one session,
on the same machine, so their numbers compare directly; each was stopped and its data deleted before
the next started. The embeddings are the ones the [pgvector run](msmarco-1m-bge-small.md) computed,
attached as input, so every store was measured on the same million vectors, queries and permissions.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Stores | Release builds, each at its own defaults and loaded through its needtoknow backend: Qdrant 1.19.2 (`create_collection`: cosine, keyword payload indexes); Elasticsearch 9.5.5 and OpenSearch 3.9.0 (`create_index`; Elasticsearch's default vector index for 384 dimensions is `bbq_hnsw`, 1-bit quantization); Weaviate 1.39.12 (`create_collection`) |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, no GPU, shared by the store and the benchmark's client threads |
| Software | Python 3.13.15, needtoknow at `5a49eec`, NumPy 2.1.3 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

## Qdrant

### Load: 1000000 chunks in qdrant

Loaded in 1046.8 s, then 730.1 s until the index was built. 1000000 points, 1000000 indexed vectors. Search breadth: the store default.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 17.0 | 34.46 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 148.43 | 541.49 | 0 |
| 1.0% | prefilter | 0.996 | 100% | 8.36 | 9.95 | 0 |
| 1.0% | postfilter | 0.858 | 56% | 114.62 | 126.48 | 0 |
| 10.0% | prefilter | 0.982 | 100% | 8.05 | 9.35 | 0 |
| 10.0% | postfilter | 0.955 | 100% | 23.04 | 57.76 | 0 |
| 100.0% | prefilter | 0.979 | 100% | 8.88 | 10.07 | 0 |
| 100.0% | postfilter | 0.979 | 100% | 8.14 | 9.42 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 122.0 | 8.05 | 9.2 | 10.16 | 0 |
| 1.0% | 2 | 166.9 | 11.83 | 14.97 | 16.54 | 0 |
| 1.0% | 4 | 190.4 | 20.38 | 30.03 | 34.96 | 0 |
| 1.0% | 8 | 198.4 | 39.19 | 59.49 | 68.77 | 0 |
| 1.0% | 16 | 200.8 | 79.26 | 105.95 | 119.62 | 0 |

## Elasticsearch

### Load: 1000000 chunks in elasticsearch

Loaded in 1511.9 s, then 1.0 s until the index was built. 1000000 documents, 2.2 GiB. Search breadth: the store default.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.935 | 100% | 31.78 | 102.14 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 233.99 | 697.78 | 0 |
| 1.0% | prefilter | 0.615 | 100% | 12.95 | 29.87 | 0 |
| 1.0% | postfilter | 0.826 | 57% | 154.13 | 193.7 | 0 |
| 10.0% | prefilter | 0.820 | 100% | 10.41 | 19.22 | 0 |
| 10.0% | postfilter | 0.929 | 100% | 30.06 | 75.97 | 0 |
| 100.0% | prefilter | 0.874 | 100% | 9.47 | 15.14 | 0 |
| 100.0% | postfilter | 0.958 | 100% | 10.0 | 11.95 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 104.2 | 9.38 | 10.99 | 12.83 | 0 |
| 1.0% | 2 | 153.8 | 12.45 | 18.64 | 21.76 | 0 |
| 1.0% | 4 | 218.6 | 17.54 | 26.14 | 33.84 | 0 |
| 1.0% | 8 | 188.5 | 34.12 | 109.11 | 149.1 | 0 |
| 1.0% | 16 | 157.6 | 49.66 | 314.12 | 1074.22 | 0 |

## OpenSearch

### Load: 1000000 chunks in opensearch

Loaded in 2287.0 s, then 2.1 s until the index was built. 1000000 documents, 1.8 GiB. Search breadth: the store default.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 47.25 | 114.02 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 167.34 | 269.06 | 0 |
| 1.0% | prefilter | 0.996 | 100% | 14.24 | 22.22 | 0 |
| 1.0% | postfilter | 0.851 | 55% | 153.19 | 166.13 | 0 |
| 10.0% | prefilter | 0.994 | 100% | 25.62 | 32.85 | 0 |
| 10.0% | postfilter | 0.962 | 100% | 30.15 | 41.33 | 0 |
| 100.0% | prefilter | 0.972 | 100% | 10.39 | 16.18 | 0 |
| 100.0% | postfilter | 0.972 | 100% | 11.0 | 12.77 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 88.8 | 10.95 | 13.17 | 14.77 | 0 |
| 1.0% | 2 | 121.1 | 16.09 | 21.49 | 24.45 | 0 |
| 1.0% | 4 | 135.3 | 28.67 | 42.05 | 49.83 | 0 |
| 1.0% | 8 | 160.3 | 47.94 | 74.8 | 88.84 | 0 |
| 1.0% | 16 | 155.4 | 97.13 | 166.65 | 249.77 | 0 |

## Weaviate

### Load: 1000000 chunks in weaviate

Loaded in 1362.5 s, then 0.0 s until the index was built. 1000000 objects; Weaviate indexes while it imports. Search breadth: the store default.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 6.55 | 7.93 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 143.67 | 164.37 | 0 |
| 1.0% | prefilter | 0.996 | 100% | 14.89 | 16.19 | 0 |
| 1.0% | postfilter | 0.847 | 56% | 139.3 | 162.3 | 0 |
| 10.0% | prefilter | 0.904 | 100% | 16.36 | 34.18 | 0 |
| 10.0% | postfilter | 0.980 | 100% | 33.42 | 71.45 | 0 |
| 100.0% | prefilter | 0.965 | 100% | 9.08 | 11.95 | 0 |
| 100.0% | postfilter | 0.984 | 100% | 12.29 | 15.32 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 66.3 | 14.91 | 16.47 | 20.08 | 0 |
| 1.0% | 2 | 118.9 | 15.86 | 24.77 | 36.18 | 0 |
| 1.0% | 4 | 209.2 | 18.43 | 24.08 | 30.83 | 0 |
| 1.0% | 8 | 230.1 | 33.3 | 53.61 | 63.48 | 0 |
| 1.0% | 16 | 221.0 | 68.07 | 124.05 | 153.28 | 0 |
