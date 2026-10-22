# FiQA: needtoknow on pgvector against PostgreSQL row-level security

Run on 2026-10-22 with [`kaggle_benchmark.ipynb`](../kaggle_benchmark.ipynb) at commit `42fcca6`
(Kaggle version 2 of the notebook, 4,331 s in total), on the corpus, embeddings, permissions and
hardware of the [full FiQA results](fiqa-qwen3-embedding-0.6b.md). That run's other rows repeated the
first run's within noise; only the new baseline is listed here.

*Row-level security* enforces the same permissions inside PostgreSQL instead of in needtoknow: a
policy on the table (`metadata -> 'allowed_groups' ?| ` the subject's groups, set per transaction),
a GIN index on the groups, the same HNSW index with iterative scans, and searches run as a role
without `BYPASSRLS`. Nothing is checked outside the database, so *Examined* does not apply.
*Complete* is the share of the 200 queries that returned 10 results. Leaks were 0 in every row.

| Layout | Readable | needtoknow prefilter: recall | p50 ms | Row-level security: recall | Complete | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| random | 0.10% | 1.000 | 4.2 | 1.000 | 100% | 3.4 | 4.3 |
| random | 1.00% | 1.000 | 11.3 | 1.000 | 100% | 10.8 | 13.2 |
| random | 5.01% | 0.974 | 14.1 | 1.000 | 100% | 42.6 | 48.4 |
| random | 20.00% | 0.965 | 6.6 | 0.998 | 100% | 164.7 | 178.0 |
| random | 100.00% | 0.978 | 5.3 | 0.998 | 100% | 608.1 | 638.3 |
| aligned | 0.14% | 0.999 | 3.4 | 0.999 | 99% | 3.3 | 4.8 |
| aligned | 1.08% | 0.998 | 11.5 | 0.999 | 100% | 10.6 | 15.9 |
| aligned | 5.21% | 0.975 | 5.8 | 0.999 | 100% | 41.5 | 51.8 |
| aligned | 20.36% | 0.971 | 4.7 | 0.998 | 100% | 155.1 | 168.3 |
| aligned | 100.00% | 0.978 | 5.3 | 0.998 | 100% | 602.1 | 635.4 |
| misaligned | 0.06% | 1.000 | 2.8 | 1.000 | 92% | 2.1 | 3.3 |
| misaligned | 0.92% | 0.999 | 9.1 | 0.999 | 100% | 8.6 | 14.0 |
| misaligned | 4.78% | 0.959 | 82.2 | 1.000 | 100% | 37.4 | 48.3 |
| misaligned | 19.89% | 0.948 | 11.1 | 0.998 | 100% | 165.4 | 182.0 |
| misaligned | 100.00% | 0.978 | 6.0 | 0.998 | 100% | 595.4 | 627.3 |

The query plans were not captured. Recall near 1.0 at every selectivity, above the HNSW index's own
0.978 when everything is readable, and latency that grows with the number of readable rows, are
what an exact scan of the rows the policy admits would produce, rather than a walk of the HNSW
index.
