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

## Run anywhere, without a model

```bash
python benchmarks/permissions_bench.py --teams 100 --docs 3000 --queries 15 --backends exact --check
```

`--dataset synthetic --embedder hashing` (the defaults) needs no model and no network. CI runs this
against real Chroma and PostgreSQL with `--check`, which fails the build if any strategy leaks,
anything errors, or a result the exact backend reports as complete is not the exact top-k.
`--help` lists every option.
