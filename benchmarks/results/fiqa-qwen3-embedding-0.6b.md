# FiQA, Qwen3-Embedding-0.6B: full results

Run on 2026-10-16 with [`kaggle_benchmark.ipynb`](../kaggle_benchmark.ipynb) at commit `18a67f0`
(Kaggle version 1 of the notebook, 3,830 s in total, of which 1,481 s embedding).

| | |
|---|---|
| Corpus | BEIR FiQA, 57,600 non-empty chunks (38 of 57,638 had no text); 200 queries |
| Embeddings | Qwen3-Embedding-0.6B, 1024 dimensions, fp16, maximum 512 tokens |
| Permissions | 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 5%, 20%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Indexes | Default settings: pgvector HNSW (m 16, ef_construction 64, ef_search 40); Chroma HNSW (cosine) |
| Hardware | Kaggle, 4 CPUs, Tesla T4 (embeddings only; all searches on CPU) |
| Software | Python 3.13.15, needtoknow 1.0.0.dev0, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0, chromadb 1.5.9, PostgreSQL 16.15, pgvector 0.8.7 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Examined* is the mean number of candidates the authorizer
checked. *Leaks* is the total number of returned chunks the subject may not read, over 200 queries
of 10 results.

### Layout: random

| Selectivity | Readable | Backend | Strategy | Recall@10 | Complete | Examined | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| 0.1% | 0.10% | exact | prefilter | 1.000 | 100% | 10 | 8.8 | 9.8 | 0 |
| 0.1% | 0.10% | exact | postfilter | 0.097 | 0% | 1000 | 47.7 | 52.3 | 0 |
| 0.1% | 0.10% | exact | naive top-k | 0.001 | 0% | 10 | 8.6 | 9.4 | 0 |
| 0.1% | 0.10% | exact | unprotected | 0.001 | 100% | 0 | 8.5 | 9.1 | 1998 |
| 0.1% | 0.10% | chroma | prefilter | 0.999 | 100% | 10 | 252.9 | 274.2 | 0 |
| 0.1% | 0.10% | chroma | postfilter | 0.097 | 0% | 1000 | 238.9 | 288.9 | 0 |
| 0.1% | 0.10% | chroma | naive top-k | 0.001 | 0% | 10 | 17.6 | 21.1 | 0 |
| 0.1% | 0.10% | pgvector | prefilter | 0.999 | 100% | 10 | 3.5 | 4.0 | 0 |
| 0.1% | 0.10% | pgvector | postfilter | 0.095 | 0% | 1000 | 102.7 | 126.9 | 0 |
| 0.1% | 0.10% | pgvector | naive top-k | 0.001 | 0% | 10 | 4.3 | 5.9 | 0 |
| 0.1% | 0.10% | pgvector, no iterative scan | prefilter | 0.999 | 100% | 10 | 3.0 | 3.5 | 0 |
| 1.0% | 1.00% | exact | prefilter | 1.000 | 100% | 10 | 8.7 | 10.1 | 0 |
| 1.0% | 1.00% | exact | postfilter | 0.858 | 52% | 960.4 | 46.6 | 50.8 | 0 |
| 1.0% | 1.00% | exact | naive top-k | 0.010 | 0% | 10 | 8.5 | 12.5 | 0 |
| 1.0% | 1.00% | exact | unprotected | 0.010 | 100% | 0 | 8.4 | 9.2 | 1980 |
| 1.0% | 1.00% | chroma | prefilter | 1.000 | 100% | 10 | 216.4 | 226.5 | 0 |
| 1.0% | 1.00% | chroma | postfilter | 0.853 | 54% | 955 | 241.2 | 286.6 | 0 |
| 1.0% | 1.00% | chroma | naive top-k | 0.010 | 0% | 10 | 17.6 | 19.8 | 0 |
| 1.0% | 1.00% | pgvector | prefilter | 1.000 | 100% | 10 | 10.4 | 11.7 | 0 |
| 1.0% | 1.00% | pgvector | postfilter | 0.849 | 52% | 953.2 | 100.4 | 127.5 | 0 |
| 1.0% | 1.00% | pgvector | naive top-k | 0.010 | 0% | 10 | 4.2 | 5.7 | 0 |
| 1.0% | 1.00% | pgvector, no iterative scan | prefilter | 1.000 | 100% | 10 | 10.1 | 12.5 | 0 |
| 5.0% | 5.01% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 9.8 | 0 |
| 5.0% | 5.01% | exact | postfilter | 1.000 | 100% | 491.2 | 32.4 | 35.2 | 0 |
| 5.0% | 5.01% | exact | naive top-k | 0.047 | 0% | 10 | 8.5 | 9.3 | 0 |
| 5.0% | 5.01% | exact | unprotected | 0.047 | 100% | 0 | 8.4 | 9.1 | 1906 |
| 5.0% | 5.01% | chroma | prefilter | 0.999 | 100% | 10 | 414.4 | 431.7 | 0 |
| 5.0% | 5.01% | chroma | postfilter | 0.989 | 100% | 498.4 | 127.3 | 152.2 | 0 |
| 5.0% | 5.01% | chroma | naive top-k | 0.045 | 0% | 10 | 17.5 | 19.8 | 0 |
| 5.0% | 5.01% | pgvector | prefilter | 0.974 | 100% | 10 | 12.7 | 20.4 | 0 |
| 5.0% | 5.01% | pgvector | postfilter | 0.992 | 100% | 493.6 | 52.6 | 70.9 | 0 |
| 5.0% | 5.01% | pgvector | naive top-k | 0.045 | 0% | 10 | 4.2 | 5.7 | 0 |
| 5.0% | 5.01% | pgvector, no iterative scan | prefilter | 0.195 | 100% | 1.9 | 4.9 | 6.4 | 0 |
| 20.0% | 20.00% | exact | prefilter | 1.000 | 100% | 10 | 9.0 | 11.9 | 0 |
| 20.0% | 20.00% | exact | postfilter | 1.000 | 100% | 128.2 | 18.9 | 20.4 | 0 |
| 20.0% | 20.00% | exact | naive top-k | 0.193 | 0% | 10 | 8.6 | 9.3 | 0 |
| 20.0% | 20.00% | exact | unprotected | 0.193 | 100% | 0 | 8.7 | 9.4 | 1614 |
| 20.0% | 20.00% | chroma | prefilter | 0.996 | 100% | 10 | 294.4 | 314.5 | 0 |
| 20.0% | 20.00% | chroma | postfilter | 0.983 | 100% | 127 | 51.4 | 61.6 | 0 |
| 20.0% | 20.00% | chroma | naive top-k | 0.193 | 0% | 10 | 17.4 | 21.2 | 0 |
| 20.0% | 20.00% | pgvector | prefilter | 0.964 | 100% | 10 | 6.1 | 8.8 | 0 |
| 20.0% | 20.00% | pgvector | postfilter | 0.976 | 100% | 128.2 | 15.6 | 20.9 | 0 |
| 20.0% | 20.00% | pgvector | naive top-k | 0.193 | 0% | 10 | 4.1 | 5.5 | 0 |
| 20.0% | 20.00% | pgvector, no iterative scan | prefilter | 0.765 | 100% | 7.8 | 4.5 | 5.8 | 0 |
| 100.0% | 100.00% | exact | prefilter | 1.000 | 100% | 10 | 9.2 | 10.2 | 0 |
| 100.0% | 100.00% | exact | postfilter | 1.000 | 100% | 40 | 8.8 | 11.7 | 0 |
| 100.0% | 100.00% | exact | naive top-k | 1.000 | 100% | 10 | 8.6 | 9.2 | 0 |
| 100.0% | 100.00% | exact | unprotected | 1.000 | 100% | 0 | 8.5 | 9.1 | 0 |
| 100.0% | 100.00% | chroma | prefilter | 0.990 | 100% | 10 | 875.9 | 920.8 | 0 |
| 100.0% | 100.00% | chroma | postfilter | 0.990 | 100% | 40 | 20.0 | 24.1 | 0 |
| 100.0% | 100.00% | chroma | naive top-k | 0.990 | 100% | 10 | 17.7 | 20.4 | 0 |
| 100.0% | 100.00% | pgvector | prefilter | 0.977 | 100% | 10 | 5.2 | 7.3 | 0 |
| 100.0% | 100.00% | pgvector | postfilter | 0.977 | 100% | 40 | 5.1 | 6.6 | 0 |
| 100.0% | 100.00% | pgvector | naive top-k | 0.977 | 100% | 10 | 4.1 | 5.4 | 0 |
| 100.0% | 100.00% | pgvector, no iterative scan | prefilter | 0.977 | 100% | 10 | 4.7 | 6.2 | 0 |

### Layout: aligned

| Selectivity | Readable | Backend | Strategy | Recall@10 | Complete | Examined | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| 0.1% | 0.14% | exact | prefilter | 1.000 | 100% | 10 | 8.7 | 9.5 | 0 |
| 0.1% | 0.14% | exact | postfilter | 0.972 | 91% | 290.8 | 18.6 | 47.0 | 0 |
| 0.1% | 0.14% | exact | naive top-k | 0.289 | 4% | 10 | 8.5 | 9.3 | 0 |
| 0.1% | 0.14% | exact | unprotected | 0.289 | 100% | 0 | 8.4 | 9.1 | 1424 |
| 0.1% | 0.14% | chroma | prefilter | 0.999 | 100% | 10 | 247.3 | 258.4 | 0 |
| 0.1% | 0.14% | chroma | postfilter | 0.969 | 92% | 287.2 | 52.7 | 237.8 | 0 |
| 0.1% | 0.14% | chroma | naive top-k | 0.290 | 4% | 10 | 17.4 | 20.8 | 0 |
| 0.1% | 0.14% | pgvector | prefilter | 0.999 | 100% | 10 | 3.3 | 5.5 | 0 |
| 0.1% | 0.14% | pgvector | postfilter | 0.969 | 92% | 289.6 | 16.6 | 108.5 | 0 |
| 0.1% | 0.14% | pgvector | naive top-k | 0.288 | 4% | 10 | 4.2 | 5.6 | 0 |
| 0.1% | 0.14% | pgvector, no iterative scan | prefilter | 0.999 | 100% | 10 | 3.0 | 4.4 | 0 |
| 1.0% | 1.08% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 11.4 | 0 |
| 1.0% | 1.08% | exact | postfilter | 0.992 | 98% | 247 | 19.0 | 36.4 | 0 |
| 1.0% | 1.08% | exact | naive top-k | 0.295 | 4% | 10 | 8.5 | 9.3 | 0 |
| 1.0% | 1.08% | exact | unprotected | 0.295 | 100% | 0 | 8.4 | 9.4 | 1409 |
| 1.0% | 1.08% | chroma | prefilter | 0.999 | 100% | 10 | 204.8 | 230.8 | 0 |
| 1.0% | 1.08% | chroma | postfilter | 0.986 | 97% | 242.8 | 51.5 | 148.7 | 0 |
| 1.0% | 1.08% | chroma | naive top-k | 0.296 | 4% | 10 | 17.6 | 20.7 | 0 |
| 1.0% | 1.08% | pgvector | prefilter | 0.996 | 100% | 10 | 11.0 | 18.2 | 0 |
| 1.0% | 1.08% | pgvector | postfilter | 0.985 | 97% | 245.2 | 15.9 | 73.7 | 0 |
| 1.0% | 1.08% | pgvector | naive top-k | 0.295 | 4% | 10 | 4.2 | 5.5 | 0 |
| 1.0% | 1.08% | pgvector, no iterative scan | prefilter | 0.955 | 100% | 9.6 | 9.7 | 15.2 | 0 |
| 5.0% | 5.21% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 12.9 | 0 |
| 5.0% | 5.21% | exact | postfilter | 1.000 | 100% | 50.2 | 9.0 | 18.7 | 0 |
| 5.0% | 5.21% | exact | naive top-k | 0.642 | 12% | 10 | 8.6 | 9.3 | 0 |
| 5.0% | 5.21% | exact | unprotected | 0.642 | 100% | 0 | 8.4 | 9.2 | 716 |
| 5.0% | 5.21% | chroma | prefilter | 0.999 | 100% | 10 | 428.4 | 449.5 | 0 |
| 5.0% | 5.21% | chroma | postfilter | 0.992 | 100% | 49 | 20.7 | 51.6 | 0 |
| 5.0% | 5.21% | chroma | naive top-k | 0.643 | 12% | 10 | 17.6 | 21.1 | 0 |
| 5.0% | 5.21% | pgvector | prefilter | 0.973 | 100% | 10 | 5.3 | 11.7 | 0 |
| 5.0% | 5.21% | pgvector | postfilter | 0.978 | 100% | 49.6 | 5.2 | 18.2 | 0 |
| 5.0% | 5.21% | pgvector | naive top-k | 0.644 | 13% | 10 | 4.2 | 5.6 | 0 |
| 5.0% | 5.21% | pgvector, no iterative scan | prefilter | 0.958 | 100% | 9.8 | 4.8 | 6.6 | 0 |
| 20.0% | 20.36% | exact | prefilter | 1.000 | 100% | 10 | 9.0 | 9.9 | 0 |
| 20.0% | 20.36% | exact | postfilter | 1.000 | 100% | 48.4 | 9.0 | 18.7 | 0 |
| 20.0% | 20.36% | exact | naive top-k | 0.549 | 7% | 10 | 8.7 | 9.5 | 0 |
| 20.0% | 20.36% | exact | unprotected | 0.549 | 100% | 0 | 8.6 | 9.3 | 902 |
| 20.0% | 20.36% | chroma | prefilter | 0.998 | 100% | 10 | 296.3 | 315.4 | 0 |
| 20.0% | 20.36% | chroma | postfilter | 0.989 | 100% | 48.4 | 20.5 | 50.1 | 0 |
| 20.0% | 20.36% | chroma | naive top-k | 0.551 | 7% | 10 | 17.4 | 20.6 | 0 |
| 20.0% | 20.36% | pgvector | prefilter | 0.971 | 100% | 10 | 4.6 | 7.0 | 0 |
| 20.0% | 20.36% | pgvector | postfilter | 0.975 | 100% | 49 | 5.1 | 17.6 | 0 |
| 20.0% | 20.36% | pgvector | naive top-k | 0.552 | 8% | 10 | 4.0 | 5.5 | 0 |
| 20.0% | 20.36% | pgvector, no iterative scan | prefilter | 0.952 | 100% | 9.8 | 4.4 | 5.8 | 0 |
| 100.0% | 100.00% | exact | prefilter | 1.000 | 100% | 10 | 9.1 | 12.0 | 0 |
| 100.0% | 100.00% | exact | postfilter | 1.000 | 100% | 40 | 9.0 | 9.8 | 0 |
| 100.0% | 100.00% | exact | naive top-k | 1.000 | 100% | 10 | 8.6 | 9.3 | 0 |
| 100.0% | 100.00% | exact | unprotected | 1.000 | 100% | 0 | 8.5 | 9.3 | 0 |
| 100.0% | 100.00% | chroma | prefilter | 0.990 | 100% | 10 | 883.9 | 938.3 | 0 |
| 100.0% | 100.00% | chroma | postfilter | 0.990 | 100% | 40 | 20.2 | 24.5 | 0 |
| 100.0% | 100.00% | chroma | naive top-k | 0.990 | 100% | 10 | 17.6 | 20.7 | 0 |
| 100.0% | 100.00% | pgvector | prefilter | 0.977 | 100% | 10 | 5.1 | 6.5 | 0 |
| 100.0% | 100.00% | pgvector | postfilter | 0.977 | 100% | 40 | 5.3 | 6.6 | 0 |
| 100.0% | 100.00% | pgvector | naive top-k | 0.977 | 100% | 10 | 4.1 | 5.5 | 0 |
| 100.0% | 100.00% | pgvector, no iterative scan | prefilter | 0.977 | 100% | 10 | 4.7 | 6.0 | 0 |

### Layout: misaligned

| Selectivity | Readable | Backend | Strategy | Recall@10 | Complete | Examined | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| 0.1% | 0.06% | exact | prefilter | 1.000 | 100% | 9.7 | 8.9 | 9.5 | 0 |
| 0.1% | 0.06% | exact | postfilter | 0.000 | 0% | 1000 | 46.8 | 51.2 | 0 |
| 0.1% | 0.06% | exact | naive top-k | 0.000 | 0% | 10 | 8.5 | 12.2 | 0 |
| 0.1% | 0.06% | exact | unprotected | 0.000 | 100% | 0 | 8.4 | 9.1 | 2000 |
| 0.1% | 0.06% | chroma | prefilter | 1.000 | 100% | 9.7 | 250.7 | 261.4 | 0 |
| 0.1% | 0.06% | chroma | postfilter | 0.000 | 0% | 1000 | 238.1 | 284.4 | 0 |
| 0.1% | 0.06% | chroma | naive top-k | 0.000 | 0% | 10 | 17.6 | 19.8 | 0 |
| 0.1% | 0.06% | pgvector | prefilter | 1.000 | 100% | 9.7 | 2.7 | 3.8 | 0 |
| 0.1% | 0.06% | pgvector | postfilter | 0.000 | 0% | 1000 | 102.2 | 126.5 | 0 |
| 0.1% | 0.06% | pgvector | naive top-k | 0.000 | 0% | 10 | 4.0 | 5.6 | 0 |
| 0.1% | 0.06% | pgvector, no iterative scan | prefilter | 1.000 | 100% | 9.7 | 2.4 | 3.5 | 0 |
| 1.0% | 0.92% | exact | prefilter | 1.000 | 100% | 10 | 8.8 | 11.4 | 0 |
| 1.0% | 0.92% | exact | postfilter | 0.049 | 0% | 1000 | 47.2 | 51.1 | 0 |
| 1.0% | 0.92% | exact | naive top-k | 0.000 | 0% | 10 | 8.7 | 11.9 | 0 |
| 1.0% | 0.92% | exact | unprotected | 0.000 | 100% | 0 | 8.5 | 9.2 | 2000 |
| 1.0% | 0.92% | chroma | prefilter | 0.999 | 100% | 10 | 238.4 | 251.0 | 0 |
| 1.0% | 0.92% | chroma | postfilter | 0.045 | 0% | 1000 | 238.6 | 287.1 | 0 |
| 1.0% | 0.92% | chroma | naive top-k | 0.000 | 0% | 10 | 17.6 | 20.5 | 0 |
| 1.0% | 0.92% | pgvector | prefilter | 0.995 | 100% | 10 | 8.4 | 14.1 | 0 |
| 1.0% | 0.92% | pgvector | postfilter | 0.048 | 0% | 1000 | 101.2 | 126.1 | 0 |
| 1.0% | 0.92% | pgvector | naive top-k | 0.000 | 0% | 10 | 4.2 | 5.6 | 0 |
| 1.0% | 0.92% | pgvector, no iterative scan | prefilter | 0.974 | 100% | 9.8 | 7.9 | 13.2 | 0 |
| 5.0% | 4.78% | exact | prefilter | 1.000 | 100% | 10 | 8.7 | 9.5 | 0 |
| 5.0% | 4.78% | exact | postfilter | 0.325 | 10% | 991 | 47.1 | 51.0 | 0 |
| 5.0% | 4.78% | exact | naive top-k | 0.000 | 0% | 10 | 8.6 | 9.3 | 0 |
| 5.0% | 4.78% | exact | unprotected | 0.000 | 100% | 0 | 8.6 | 9.5 | 2000 |
| 5.0% | 4.78% | chroma | prefilter | 0.998 | 100% | 10 | 444.5 | 464.7 | 0 |
| 5.0% | 4.78% | chroma | postfilter | 0.307 | 8% | 994.6 | 236.0 | 290.4 | 0 |
| 5.0% | 4.78% | chroma | naive top-k | 0.000 | 0% | 10 | 17.8 | 19.7 | 0 |
| 5.0% | 4.78% | pgvector | prefilter | 0.950 | 100% | 10 | 86.9 | 140.6 | 0 |
| 5.0% | 4.78% | pgvector | postfilter | 0.314 | 10% | 992.8 | 100.2 | 125.2 | 0 |
| 5.0% | 4.78% | pgvector | naive top-k | 0.000 | 0% | 10 | 4.2 | 5.9 | 0 |
| 5.0% | 4.78% | pgvector, no iterative scan | prefilter | 0.073 | 100% | 0.7 | 5.0 | 40.1 | 0 |
| 20.0% | 19.89% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 9.6 | 0 |
| 20.0% | 19.89% | exact | postfilter | 1.000 | 100% | 365.8 | 20.1 | 34.6 | 0 |
| 20.0% | 19.89% | exact | naive top-k | 0.041 | 0% | 10 | 8.6 | 11.6 | 0 |
| 20.0% | 19.89% | exact | unprotected | 0.041 | 100% | 0 | 8.5 | 9.2 | 1918 |
| 20.0% | 19.89% | chroma | prefilter | 0.997 | 100% | 10 | 296.8 | 316.1 | 0 |
| 20.0% | 19.89% | chroma | postfilter | 0.968 | 100% | 373.6 | 61.7 | 153.1 | 0 |
| 20.0% | 19.89% | chroma | naive top-k | 0.040 | 0% | 10 | 17.5 | 19.8 | 0 |
| 20.0% | 19.89% | pgvector | prefilter | 0.949 | 100% | 10 | 10.3 | 15.7 | 0 |
| 20.0% | 19.89% | pgvector | postfilter | 0.974 | 100% | 371.2 | 21.3 | 60.4 | 0 |
| 20.0% | 19.89% | pgvector | naive top-k | 0.041 | 0% | 10 | 4.2 | 5.9 | 0 |
| 20.0% | 19.89% | pgvector, no iterative scan | prefilter | 0.218 | 100% | 2.2 | 4.1 | 5.6 | 0 |
| 100.0% | 100.00% | exact | prefilter | 1.000 | 100% | 10 | 9.1 | 9.8 | 0 |
| 100.0% | 100.00% | exact | postfilter | 1.000 | 100% | 40 | 9.1 | 10.2 | 0 |
| 100.0% | 100.00% | exact | naive top-k | 1.000 | 100% | 10 | 8.8 | 11.3 | 0 |
| 100.0% | 100.00% | exact | unprotected | 1.000 | 100% | 0 | 8.6 | 9.2 | 0 |
| 100.0% | 100.00% | chroma | prefilter | 0.990 | 100% | 10 | 888.9 | 940.9 | 0 |
| 100.0% | 100.00% | chroma | postfilter | 0.990 | 100% | 40 | 22.1 | 24.3 | 0 |
| 100.0% | 100.00% | chroma | naive top-k | 0.990 | 100% | 10 | 18.7 | 20.8 | 0 |
| 100.0% | 100.00% | pgvector | prefilter | 0.977 | 100% | 10 | 5.2 | 7.1 | 0 |
| 100.0% | 100.00% | pgvector | postfilter | 0.977 | 100% | 40 | 5.2 | 6.7 | 0 |
| 100.0% | 100.00% | pgvector | naive top-k | 0.977 | 100% | 10 | 4.1 | 5.5 | 0 |
| 100.0% | 100.00% | pgvector, no iterative scan | prefilter | 0.977 | 100% | 10 | 4.9 | 6.5 | 0 |
