# Branch `feat/eval`

Adds `src/company_recs/eval.py`, an offline evaluation of recommendation quality against category match, and
wires it to `just eval`. Results are written to `eval/results.md`.

## What changed

| File | What it does |
|---|---|
| `src/company_recs/eval.py` | Loads the active companies, runs the `random`, `hashed-bow` and `bge-small` retrievers, computes P@4, NDCG@4 and MRR, and writes `eval/results.md` with a metrics table and 5 sample queries. Exits 1 if bge-small's NDCG@4 is not above hashed-bow's. |
| `eval/results.md` | The results on the real feed (2,000 companies). |
| `tests/test_eval.py` | The metric arithmetic, the hashed bag of words, the random baseline and rendering (unit); a full run against Postgres that passes the guard, one that fails it, and the query filters (integration). |
| `Justfile` | `just eval` runs `python -m company_recs.eval`. |
| `DESIGN.md`, `SETUP.md` | An Evaluation section; how to run it. |

## How it works

- **Queries:** active companies with `description <> '' AND length(description) > 10` that have a category.
- **Candidates:** every active company with an embedding, the set the API recommends from.
- **Relevance:** a neighbour is relevant iff its category equals the query's.
- **Metrics:** P@4 is relevant / returned. NDCG@4 uses binary gains, a `1 / log2(i + 2)` discount and an ideal
  ranking of `min(4, relevant available)`. MRR is 1 / rank of the first relevant neighbour within the top 4,
  else 0.
- **`random`:** 4 other candidates per query from `random.Random(20261125)`; `--seed` changes it.
- **`hashed-bow`:** each token goes to bucket `int(sha256(token)) % 384` with weight 1, then the vector is
  L2-normalised. Ranking is exact cosine in NumPy, and ties go to the lower id.
- **`bge-small`:** calls `api.find_similar(connection, id, 4)`. That is the API's own SQL with self-exclusion,
  `is_active` and the HNSW index with iterative scan, not a copy of it.

## Results

| Retriever | P@4 | NDCG@4 | MRR | N |
|---|---:|---:|---:|---:|
| random | 0.0880 | 0.0891 | 0.1740 | 1855 |
| hashed-bow | 0.9454 | 0.9397 | 0.9554 | 1855 |
| bge-small | 0.9523 | 0.9498 | 0.9712 | 1855 |

With exact-text twins removed, NDCG@4 is 0.0820 for random, 0.3385 for hashed-bow and 0.5127 for bge-small.
The guard passes: 0.9498 > 0.9397. Two runs produce byte-identical `eval/results.md`, and a run takes about 20 s.

## Where this differs from the spec

- **Queries without a category are left out (98 of them).** No neighbour could be relevant to them, so they
  would only pull every retriever's score down by the same amount. A neighbour with no category is never
  relevant. The report states both counts.
- **Tokens are lower-cased before hashing.** The spec says "each whitespace token". Without lower-casing,
  "Retail" at the start of a description and "retail" in the middle land in different buckets, which makes
  the baseline weaker than it needs to be. A weak baseline makes the guard easier to pass, which is the
  opposite of what a guard is for.
- **The bag of words is built from the same text bge-small embedded** (`build_embed_text`). That is the
  description, or `name | category | state` for the 39 active companies that have none. Those companies are
  never queries (their description is empty), but they are candidates for both retrievers on equal terms.
- **The bag of words is ranked exactly in NumPy**, not with an index. At 2,000 rows that's instant, and an
  exact baseline is the fair one to compare against.
- **Added: a second table with exact-text twins removed.** 1,651 of the 1,855 queries have at least 4 other
  companies with exactly the same description, so the spec's table mostly measures "did it find the copies".
  That's why bge-small leads by only 0.01 NDCG@4 there. Removing each query's twins from its candidates (and
  from the NDCG ideal) shows the real gap: 0.51 against 0.34. The spec's table and the guard are unchanged.
- **The sample queries are bge-small's top 4**, the same as the API returns. The 5 hand-picked companies cover
  5 categories, 4 states, and both English and Malay descriptions: SYN-000010 (Agriculture), SYN-000024
  (Education, Malay), SYN-000025 (Hospitality), SYN-000012 (Manufacturing, Malay) and SYN-000017 (Healthcare).
  All of their neighbours score 1.0 because they are twins. SYN-000024's first neighbour has no category, so it
  counts as a miss.
- **`--output` and `--seed` options**, so tests can write to a temporary file.
