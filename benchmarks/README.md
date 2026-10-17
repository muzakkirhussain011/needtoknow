# Benchmarks

`permissions_bench.py` measures permission-aware retrieval with real embeddings: for each backend,
strategy and permission selectivity, how many of the true top-k permitted chunks come back
(recall@k), whether any forbidden chunk does (leaks), how often the result is complete, and how
long a retrieval takes. Results and their discussion are in the
[documentation](../docs/benchmarks.md); the full tables of each run are in [`results/`](results).

## What is compared

| Backend | Search |
|---|---|
| `exact` | Brute-force cosine similarity over all vectors (NumPy), with exact filtering |
| `chroma` | Chroma's HNSW index, filter as a `where` clause |
| `pgvector` | PostgreSQL + pgvector HNSW index, filter in the SQL query, iterative index scans |
| `pgvector, no iterative scan` | The same index with `hnsw.iterative_scan` off, as before pgvector 0.8 |

Strategies: needtoknow's `prefilter` and `postfilter`, and two baselines that needtoknow exists to
replace: `naive top-k` (take the top k, then drop what the user may not read) and `unprotected`
(no permission check at all).

Ground truth is computed independently of needtoknow: the exact top-k by cosine similarity among
the chunks the subject may read.

## Permissions

Permissions are hierarchical, as in most organisations. Every chunk belongs to one team; ten
teams form a department and a hundred a division, and the chunk's `allowed_groups` lists all
three. A subject with selectivity *s* may read a share *s* of the teams, expressed with the fewest
groups (one team, one department, two divisions, ...), so filters stay as small as real ones.

| Layout | Teams | Subject's groups | Models |
|---|---|---|---|
| `random` | random | random | permissions unrelated to content |
| `aligned` | topical: chunks nearest the same anchor | nearest the query | people searching their own department's documents |
| `misaligned` | topical | farthest from the query | the hard case for filtered approximate search |

## Run on Kaggle

The embeddings need a GPU, so the full run happens on Kaggle, not on a laptop.

1. Import [`kaggle_benchmark.ipynb`](kaggle_benchmark.ipynb) into Kaggle (File, Import Notebook,
   paste its GitHub URL).
2. In the notebook settings, choose a GPU accelerator and turn Internet on.
3. Run all cells. `kaggle_setup.sh` installs PostgreSQL and builds pgvector; the benchmark then
   writes `results.json` and `results.md` to `/kaggle/working`.

## Load benchmark

`load_bench.py` measures needtoknow at scale on PostgreSQL + pgvector: the first million passages
of BEIR MS MARCO with real MS MARCO questions, embedded with BAAI/bge-small-en-v1.5. It reports
how long loading and indexing take, recall and latency by permission selectivity, throughput as
concurrent users grow, and what Open Policy Agent costs as the permission service, with and without
`CachingAuthorizer`. Run it with [`kaggle_load.ipynb`](kaggle_load.ipynb), which also starts OPA
with the example policy and gives PostgreSQL enough memory for a million rows.

`--backend` runs the same measurements on another store: `qdrant`, `milvus`, `elasticsearch`,
`opensearch` or `weaviate`, at `--url` (Weaviate also takes `--grpc-port`). The corpus is loaded
through needtoknow's own backend for that store, so the layout measured is the one you get, and the
load time includes waiting until the store's index is built. `--ef-search` sets each store's search
breadth (see [`stores.py`](stores.py)), and `--index-type` sets Elasticsearch's vector index type
(`int8_hnsw`, `hnsw`; its default for 384 dimensions is `bbq_hnsw`). On Kaggle, which has no Docker,
[`kaggle_stores.ipynb`](kaggle_stores.ipynb) runs a plan of runs in turn, each store started from its
release build by [`kaggle_stores.sh`](kaggle_stores.sh) and stopped before the next: `every-store`
measures each store at its defaults, `elasticsearch-index-types` Elasticsearch with other index
types. Milvus needs Docker or Kubernetes, so it is measured elsewhere.

CI runs a small synthetic version against real PostgreSQL and OPA, and against every other store,
with `--check`, which fails on any leak or error. `--render load_results.json` prints the tables of
a saved run again without running anything.

## Run anywhere, without a model

```bash
python benchmarks/permissions_bench.py --teams 100 --docs 3000 --queries 15 --backends exact --check
```

`--dataset synthetic --embedder hashing` (the defaults) needs no model and no network. CI runs this
against real Chroma and PostgreSQL with `--check`, which fails the build if any strategy leaks,
anything errors, or a result the exact backend reports as complete is not the exact top-k.
`--help` lists every option.
