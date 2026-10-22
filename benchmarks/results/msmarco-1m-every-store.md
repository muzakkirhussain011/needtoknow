# MS MARCO, a million chunks, bge-small-en-v1.5, six stores: load benchmark results

Run on 2026-10-22 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) (plan `all-stores`, Kaggle
version 7 of the notebook, 10,777 s in total), on main with the 1.4.0 code. The six stores ran one
after another in one session, on the same machine, so their numbers compare directly; each was
stopped and its data deleted before the next started. The embeddings are the ones the
[pgvector run](msmarco-1m-bge-small.md) computed, attached as input, so every store was measured on
the same million vectors, queries and permissions.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Stores | Release builds, each at its own defaults and loaded through its needtoknow backend: Qdrant 1.19.2, Elasticsearch 9.5.5 (default vector index for 384 dimensions: `bbq_hnsw`, 1-bit quantization), OpenSearch 3.9.0, Weaviate 1.39.12, LanceDB 0.40 (embedded; default IVF_PQ index), Redis 8.10.2 (HNSW, M 16, EF_CONSTRUCTION 200) |
| Load | Throughput: users pre-filtering at 1% readable, cycling through the 300 questions; each setting run 3 times for 20 s; every returned chunk checked against the subject's permissions |
| Hardware | Kaggle, 4 CPUs, no GPU, shared by the store and the benchmark's client threads |
| Software | Python 3.13.15, NumPy 2.1.3 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the number of returned chunks the subject may not read.
In the throughput tables each row is the median of the 3 runs, with the slowest and fastest run in
brackets; errors, chunks checked and leaks are totals over the runs.

## Qdrant

Loaded in 480.4 s, then 555.4 s until the index was built. 1,000,000 points, 1,000,000 indexed
vectors.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 5.31 | 6.32 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 113.69 | 137.56 | 0 |
| 1% | prefilter | 0.996 | 100% | 6.74 | 8.08 | 0 |
| 1% | postfilter | 0.852 | 54% | 98.45 | 106.16 | 0 |
| 10% | prefilter | 0.977 | 100% | 5.93 | 7.44 | 0 |
| 10% | postfilter | 0.960 | 100% | 18.14 | 48.90 | 0 |
| 100% | prefilter | 0.977 | 100% | 6.78 | 8.41 | 0 |
| 100% | postfilter | 0.977 | 100% | 6.34 | 7.15 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 159.4 (155.1 to 161.4) | 6.11 | 7.20 | 7.86 | 0 | 95,180 | 0 |
| 2 | 204.8 (154.4 to 207.2) | 9.34 | 13.67 | 21.51 | 0 | 113,330 | 0 |
| 4 | 253.6 (253.0 to 256.1) | 15.55 | 21.84 | 24.63 | 0 | 152,630 | 0 |
| 8 | 263.9 (262.9 to 266.2) | 29.48 | 45.65 | 52.90 | 0 | 158,710 | 0 |
| 16 | 266.1 (265.9 to 268.6) | 59.62 | 78.41 | 92.00 | 2 | 160,370 | 0 |

## Elasticsearch

Loaded in 1,198.1 s, then 1.1 s until the index was built. 1,000,000 documents, 1.9 GiB.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.936 | 100% | 16.57 | 30.39 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 153.44 | 217.77 | 0 |
| 1% | prefilter | 0.628 | 100% | 11.34 | 36.15 | 0 |
| 1% | postfilter | 0.826 | 54% | 187.62 | 659.10 | 0 |
| 10% | prefilter | 0.814 | 100% | 14.69 | 26.59 | 0 |
| 10% | postfilter | 0.932 | 100% | 27.50 | 71.71 | 0 |
| 100% | prefilter | 0.875 | 100% | 8.23 | 13.07 | 0 |
| 100% | postfilter | 0.954 | 100% | 8.61 | 11.01 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 117.3 (110.9 to 120.8) | 8.34 | 10.14 | 12.02 | 0 | 69,830 | 0 |
| 2 | 161.6 (152.5 to 166.6) | 12.14 | 14.78 | 17.28 | 0 | 96,180 | 0 |
| 4 | 208.1 (206.7 to 209.1) | 18.46 | 27.48 | 33.50 | 0 | 124,870 | 0 |
| 8 | 235.7 (233.5 to 237.4) | 32.34 | 52.89 | 64.80 | 0 | 141,420 | 0 |
| 16 | 233.7 (231.9 to 235.3) | 41.44 | 70.26 | 114.67 | 0 | 140,460 | 0 |

## OpenSearch

Loaded in 1,764.6 s, then 1.3 s until the index was built. 1,000,000 documents, 1.7 GiB.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 14.76 | 26.40 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 143.88 | 200.32 | 0 |
| 1% | prefilter | 0.996 | 100% | 13.62 | 19.50 | 0 |
| 1% | postfilter | 0.852 | 55% | 138.01 | 148.30 | 0 |
| 10% | prefilter | 0.993 | 100% | 21.69 | 28.16 | 0 |
| 10% | postfilter | 0.965 | 100% | 25.85 | 33.35 | 0 |
| 100% | prefilter | 0.971 | 100% | 9.03 | 15.39 | 0 |
| 100% | postfilter | 0.971 | 100% | 9.19 | 10.49 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 0.9 (0.2 to 58.4) | 1,065.28 | 2,588.82 | 2,936.43 | 0 | 13,130 | 0 |
| 2 | 142.2 (101.0 to 151.7) | 13.54 | 18.96 | 22.29 | 0 | 78,990 | 0 |
| 4 | 176.6 (174.2 to 177.6) | 22.40 | 31.90 | 37.23 | 0 | 105,730 | 0 |
| 8 | 190.1 (190.0 to 192.4) | 40.88 | 62.46 | 75.23 | 0 | 114,640 | 0 |
| 16 | 195.8 (195.2 to 197.0) | 79.58 | 126.60 | 152.96 | 0 | 117,830 | 0 |

The first throughput setting ran on a cold store, straight after the one-query-at-a-time
measurements: two of its three runs averaged under 1 request a second, the third 58.4.

## Weaviate

Loaded in 1,146.7 s; Weaviate indexes while it imports. 1,000,000 objects.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 4.18 | 5.40 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 122.00 | 137.79 | 0 |
| 1% | prefilter | 0.996 | 100% | 11.58 | 12.84 | 0 |
| 1% | postfilter | 0.846 | 56% | 118.45 | 132.18 | 0 |
| 10% | prefilter | 0.907 | 100% | 10.63 | 14.82 | 0 |
| 10% | postfilter | 0.980 | 100% | 25.74 | 36.94 | 0 |
| 100% | prefilter | 0.965 | 100% | 6.26 | 7.85 | 0 |
| 100% | postfilter | 0.984 | 100% | 9.11 | 11.71 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 57.2 (55.4 to 85.7) | 11.92 | 59.80 | 70.33 | 0 | 39,660 | 0 |
| 2 | 155.0 (154.0 to 156.4) | 12.52 | 14.53 | 17.27 | 0 | 93,110 | 0 |
| 4 | 254.3 (252.7 to 255.8) | 15.13 | 20.16 | 23.58 | 0 | 152,630 | 0 |
| 8 | 268.1 (250.4 to 273.3) | 28.50 | 47.78 | 57.99 | 0 | 158,510 | 0 |
| 16 | 277.0 (271.7 to 278.5) | 54.27 | 101.71 | 122.80 | 0 | 165,680 | 0 |

## LanceDB

Bulk-loaded with `table.add` in 17.8 s, then 93.8 s to build the indexes; LanceDB's default vector
index (IVF_PQ), cosine. Embedded: the store runs in the benchmark's own process.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.510 | 100% | 18.94 | 94.31 | 0 |
| 0.1% | postfilter | 0.082 | 0% | 105.28 | 177.81 | 0 |
| 1% | prefilter | 0.474 | 100% | 20.11 | 22.78 | 0 |
| 1% | postfilter | 0.441 | 51% | 106.06 | 114.65 | 0 |
| 10% | prefilter | 0.474 | 100% | 20.16 | 22.58 | 0 |
| 10% | postfilter | 0.474 | 100% | 33.85 | 39.81 | 0 |
| 100% | prefilter | 0.517 | 100% | 28.22 | 31.86 | 0 |
| 100% | postfilter | 0.517 | 100% | 15.44 | 16.96 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 49.1 (48.3 to 49.4) | 20.19 | 22.79 | 24.41 | 0 | 29,380 | 0 |
| 2 | 76.8 (75.2 to 78.3) | 25.43 | 31.58 | 33.86 | 0 | 46,090 | 0 |
| 4 | 108.6 (106.2 to 110.6) | 36.19 | 45.02 | 50.29 | 0 | 65,160 | 0 |
| 8 | 116.2 (115.5 to 117.9) | 67.89 | 87.75 | 99.87 | 0 | 70,080 | 0 |
| 16 | 118.3 (117.4 to 118.3) | 133.61 | 172.60 | 194.48 | 0 | 71,120 | 0 |

## Redis

Loaded in 529.2 s; the index (HNSW, M 16, EF_CONSTRUCTION 200) builds while documents arrive.
1,000,000 documents.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 2.77 | 3.72 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 123.78 | 129.94 | 0 |
| 1% | prefilter | 0.996 | 100% | 13.01 | 13.53 | 0 |
| 1% | postfilter | 0.844 | 56% | 122.61 | 129.10 | 0 |
| 10% | prefilter | 0.897 | 100% | 2.51 | 3.81 | 0 |
| 10% | postfilter | 0.926 | 100% | 12.61 | 14.13 | 0 |
| 100% | prefilter | 0.769 | 100% | 1.55 | 2.15 | 0 |
| 100% | postfilter | 0.897 | 100% | 2.92 | 3.19 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 76.1 (75.6 to 76.8) | 13.07 | 13.58 | 14.16 | 0 | 45,700 | 0 |
| 2 | 149.2 (149.2 to 149.7) | 13.14 | 14.92 | 15.50 | 0 | 89,630 | 0 |
| 4 | 253.2 (248.8 to 253.5) | 15.32 | 19.11 | 21.91 | 0 | 151,180 | 0 |
| 8 | 260.2 (242.8 to 276.7) | 29.00 | 40.61 | 72.36 | 0 | 156,210 | 0 |
| 16 | 275.7 (273.1 to 275.9) | 57.52 | 64.31 | 72.98 | 0 | 165,380 | 0 |
