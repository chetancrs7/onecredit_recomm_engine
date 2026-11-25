"""Offline evaluation of recommendation quality, with category match as the ground truth.

Every active company with a description longer than 10 characters is a query. A returned neighbour
is relevant when its category equals the query's. Three retrievers run over the same companies:

- `random`: 4 other active companies drawn at random (seeded), the floor.
- `hashed-bow`: a 384-dim bag of words, each lower-cased whitespace token hashed with SHA-256
  into one bucket with weight 1, L2-normalised, ranked by exact cosine similarity.
- `bge-small`: the stored `embedding` column, queried with the API's own `find_similar`, so the
  SQL, the self-exclusion, the `is_active` filter and the HNSW index are the ones in production.

The feed reuses descriptions heavily (174 distinct descriptions for 2,000 companies), so most
queries have several companies with exactly the same text, and any text retriever finds those
first. A second table therefore repeats the eval with those exact-text twins removed from each
query's candidates, which measures what a retriever adds beyond string matching.

Run with `just eval` or `python -m company_recs.eval`. Results go to `eval/results.md`. The exit
code is 1 if bge-small's NDCG@4 (first table) is not above hashed-bow's, a guard that the eval
really ran against real embeddings.
"""

import argparse
import hashlib
import math
import random
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

from company_recs import db
from company_recs.api import find_similar
from company_recs.embedder import EMBED_DIM, MODEL_NAME, build_embed_text

K: Final = 4
MIN_DESCRIPTION_CHARS: Final = 10
DEFAULT_SEED: Final = 20261125
RESULTS_PATH: Final = Path(__file__).resolve().parents[2] / "eval" / "results.md"

SAMPLE_QUERY_IDS: Final = (
    "SYN-000010",
    "SYN-000024",
    "SYN-000025",
    "SYN-000012",
    "SYN-000017",
)

RANDOM: Final = "random"
HASHED_BOW: Final = "hashed-bow"
BGE_SMALL: Final = "bge-small"

_CANDIDATES_SQL: Final = """
SELECT id, name, category, state, description,
       description <> '' AND length(description) > %(min_chars)s
FROM companies
WHERE is_active AND embedding IS NOT NULL
ORDER BY id
"""


@dataclass(frozen=True)
class Company:
    """An active company with an embedding: a candidate, and a query if `is_query`."""

    id: str
    name: str
    category: str | None
    state: str | None
    description: str | None
    is_query: bool

    @property
    def text(self) -> str:
        """The same text the consumer embeds with bge-small."""
        return build_embed_text(self.name, self.category, self.state, self.description)


@dataclass(frozen=True)
class Neighbour:
    company: Company
    score: float | None


type Retriever = Callable[[Company], list[Neighbour]]
type Skip = Callable[[Company, Company], bool]


def keep_all(query: Company, candidate: Company) -> bool:
    return False


def same_text(query: Company, candidate: Company) -> bool:
    """A twin: a candidate whose embedded text is exactly the query's."""
    return candidate.text == query.text


@dataclass(frozen=True)
class Scores:
    precision: float
    ndcg: float
    mrr: float
    n: int


@dataclass(frozen=True)
class Report:
    candidates: int
    queries: int
    without_category: int
    without_relevant: int
    twin_queries: int
    seed: int
    scores: dict[str, Scores]
    scores_without_twins: dict[str, Scores]
    samples: list[tuple[Company, list[Neighbour]]]
    missing_samples: list[str]


def precision_at_k(relevant: Sequence[bool]) -> float:
    """The fraction of the returned neighbours that are relevant."""
    return sum(relevant) / len(relevant) if relevant else 0.0


def dcg(relevant: Sequence[bool]) -> float:
    """Discounted cumulative gain with binary relevance and a 1 / log2(rank + 1) discount."""
    return sum(1.0 / math.log2(i + 2) for i, is_relevant in enumerate(relevant) if is_relevant)


def ndcg_at_k(relevant: Sequence[bool], total_relevant: int, k: int = K) -> float:
    """DCG of the top `k` over the DCG of an ideal ranking.

    The ideal ranking puts `min(k, total_relevant)` relevant companies first, so a query whose
    category has fewer than `k` other members can still score 1.
    """
    ideal = dcg([True] * min(k, total_relevant))
    return dcg(relevant[:k]) / ideal if ideal else 0.0


def reciprocal_rank(relevant: Sequence[bool]) -> float:
    """1 / rank of the first relevant neighbour, or 0 if none of them is relevant."""
    return next((1.0 / (i + 1) for i, is_relevant in enumerate(relevant) if is_relevant), 0.0)


def is_relevant(query: Company, neighbour: Company) -> bool:
    return query.category is not None and neighbour.category == query.category


def hashed_bow_vector(text: str, dim: int = EMBED_DIM) -> np.ndarray:
    """Hash each lower-cased whitespace token into one of `dim` buckets, then L2-normalise."""
    vector = np.zeros(dim)
    for token in text.lower().split():
        bucket = int.from_bytes(hashlib.sha256(token.encode()).digest()) % dim
        vector[bucket] += 1.0
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def random_retriever(
    candidates: Sequence[Company], seed: int, k: int = K, skip: Skip = keep_all
) -> Retriever:
    """Draw `k` other candidates at random for each query. Seeded, so runs repeat."""
    rng = random.Random(seed)

    def retrieve(query: Company) -> list[Neighbour]:
        pool = [c for c in candidates if c.id != query.id and not skip(query, c)]
        return [Neighbour(company, None) for company in rng.sample(pool, min(k, len(pool)))]

    return retrieve


def hashed_bow_retriever(
    candidates: Sequence[Company], k: int = K, skip: Skip = keep_all
) -> Retriever:
    """Rank every other candidate by exact cosine similarity of hashed bag-of-words vectors.

    Ties go to the lower company id, so the ranking is deterministic.
    """
    ordered = sorted(candidates, key=lambda company: company.id)
    matrix = np.stack([hashed_bow_vector(company.text) for company in ordered])
    row_of = {company.id: row for row, company in enumerate(ordered)}

    def retrieve(query: Company) -> list[Neighbour]:
        row = row_of[query.id]
        scores = matrix @ matrix[row]
        scores[row] = -np.inf
        for i, company in enumerate(ordered):
            if skip(query, company):
                scores[i] = -np.inf
        ranked: list[int] = np.argsort(-scores, kind="stable")[:k].tolist()
        top = [i for i in ranked if scores[i] > -np.inf]
        return [Neighbour(ordered[i], round(float(scores[i]), 4)) for i in top]

    return retrieve


def bge_small_retriever(
    connection: db.Connection, candidates: Sequence[Company], k: int = K, skip: Skip = keep_all
) -> Retriever:
    """The API's `find_similar` over the stored bge-small embeddings.

    With `skip`, it asks for as many extra neighbours as there are skipped candidates, then drops
    those.
    """
    by_id = {company.id: company for company in candidates}

    def retrieve(query: Company) -> list[Neighbour]:
        skipped = sum(1 for c in candidates if c.id != query.id and skip(query, c))
        _, similar = find_similar(connection, query.id, k + skipped)
        kept = [c for c in similar if not skip(query, by_id[c.id])]
        return [Neighbour(by_id[company.id], company.score) for company in kept[:k]]

    return retrieve


def evaluate(
    queries: Sequence[Company], retrieve: Retriever, total_relevant: Callable[[Company], int]
) -> Scores:
    """Mean P@k, NDCG@k and MRR of `retrieve` over `queries`.

    `total_relevant(query)` is how many relevant candidates the query could have been given.
    """
    precision = ndcg = mrr = 0.0
    for query in queries:
        relevant = [is_relevant(query, neighbour.company) for neighbour in retrieve(query)]
        precision += precision_at_k(relevant)
        ndcg += ndcg_at_k(relevant, total_relevant(query))
        mrr += reciprocal_rank(relevant)
    n = len(queries)
    return Scores(precision / n, ndcg / n, mrr / n, n) if n else Scores(0.0, 0.0, 0.0, 0)


def load_candidates(connection: db.Connection) -> list[Company]:
    """Every active company with an embedding, the same set the API recommends from."""
    rows = connection.execute(_CANDIDATES_SQL, {"min_chars": MIN_DESCRIPTION_CHARS}).fetchall()
    return [Company(*row[:5], is_query=bool(row[5])) for row in rows]


def run(
    connection: db.Connection,
    seed: int = DEFAULT_SEED,
    sample_ids: Sequence[str] = SAMPLE_QUERY_IDS,
) -> Report:
    """Evaluate the three retrievers on the companies in the database, with and without twins."""
    candidates = load_candidates(connection)
    in_category = Counter(company.category for company in candidates)
    twins_in_category = Counter((company.category, company.text) for company in candidates)
    with_text = Counter(company.text for company in candidates)

    with_description = [company for company in candidates if company.is_query]
    with_category = [company for company in with_description if company.category is not None]
    queries = [company for company in with_category if in_category[company.category] > 1]

    def all_relevant(query: Company) -> int:
        return in_category[query.category] - 1

    def relevant_non_twins(query: Company) -> int:
        return in_category[query.category] - twins_in_category[(query.category, query.text)]

    def retrievers(skip: Skip) -> dict[str, Retriever]:
        return {
            RANDOM: random_retriever(candidates, seed, skip=skip),
            HASHED_BOW: hashed_bow_retriever(candidates, skip=skip),
            BGE_SMALL: bge_small_retriever(connection, candidates, skip=skip),
        }

    scores = {
        name: evaluate(queries, retrieve, all_relevant)
        for name, retrieve in retrievers(keep_all).items()
    }
    scores_without_twins = {
        name: evaluate(queries, retrieve, relevant_non_twins)
        for name, retrieve in retrievers(same_text).items()
    }

    bge_small = bge_small_retriever(connection, candidates)
    by_id = {company.id: company for company in candidates}
    return Report(
        candidates=len(candidates),
        queries=len(queries),
        without_category=len(with_description) - len(with_category),
        without_relevant=len(with_category) - len(queries),
        twin_queries=sum(1 for query in queries if with_text[query.text] > K),
        seed=seed,
        scores=scores,
        scores_without_twins=scores_without_twins,
        samples=[(by_id[i], bge_small(by_id[i])) for i in sample_ids if i in by_id],
        missing_samples=[i for i in sample_ids if i not in by_id],
    )


def _cell(value: str | None) -> str:
    return "—" if value is None else value.replace("|", "\\|")


def _table(scores: dict[str, Scores]) -> list[str]:
    lines = [f"| Retriever | P@{K} | NDCG@{K} | MRR | N |", "|---|---:|---:|---:|---:|"]
    for name, s in scores.items():
        lines.append(f"| {name} | {s.precision:.4f} | {s.ndcg:.4f} | {s.mrr:.4f} | {s.n} |")
    return lines


def render(report: Report) -> str:
    """The report as Markdown: both metrics tables, then the sample queries."""
    lines = [
        "# Evaluation results",
        "",
        "Written by `just eval` (`python -m company_recs.eval`) from the companies in Postgres.",
        "",
        "- **Ground truth:** a neighbour is relevant when its category equals the query's.",
        f"- **Queries:** {report.queries} active companies with a description longer than "
        f"{MIN_DESCRIPTION_CHARS} characters. Left out: {report.without_category} with no "
        f"category, {report.without_relevant} whose category has no other active member.",
        f"- **Candidates:** {report.candidates} active companies with an embedding, the set the "
        "API recommends from.",
        f"- **Metrics:** k = {K}. P@{K} is the fraction of the {K} neighbours that are relevant; "
        f"NDCG@{K} uses binary relevance and a 1 / log2(rank + 1) discount; MRR is 1 / rank of "
        f"the first relevant neighbour in the top {K}, else 0. All are means over the queries.",
        f"- **Retrievers:** `{RANDOM}` (seed {report.seed}); `{HASHED_BOW}` (SHA-256 token "
        f"hashing into {EMBED_DIM} buckets, exact cosine); `{BGE_SMALL}` ({MODEL_NAME}, the "
        "stored embeddings through the API's SQL and HNSW index).",
        "",
        *_table(report.scores),
        "",
        "## Without exact-text twins",
        "",
        f"{report.twin_queries} of the {report.queries} queries have at least {K} other "
        "companies with exactly the same description, so the table above mostly measures "
        "whether a retriever finds those. Here each query's twins are removed from its "
        f"candidates and from the ideal ranking for NDCG@{K}, which shows how well a retriever "
        "does beyond string matching.",
        "",
        *_table(report.scores_without_twins),
        "",
        "## Sample queries",
        "",
        f"Top {K} from `{BGE_SMALL}`, as the API returns them.",
    ]
    for query, neighbours in report.samples:
        lines += [
            "",
            f"### {query.id} · {_cell(query.name)}",
            "",
            f'{_cell(query.category)} · {_cell(query.state)} · "{_cell(query.description)}"',
            "",
            "| # | Name | Category | State | Score |",
            "|---:|---|---|---|---:|",
        ]
        for rank, neighbour in enumerate(neighbours, start=1):
            company = neighbour.company
            score = "—" if neighbour.score is None else f"{neighbour.score:.4f}"
            lines.append(
                f"| {rank} | {_cell(company.name)} | {_cell(company.category)} "
                f"| {_cell(company.state)} | {score} |"
            )
    if report.missing_samples:
        missing = ", ".join(report.missing_samples)
        lines += ["", f"Not in the database or struck off, so not shown: {missing}."]
    return "\n".join(lines) + "\n"


def passes_guard(report: Report) -> bool:
    """bge-small must beat the hashed bag of words on NDCG@4."""
    return report.scores[BGE_SMALL].ndcg > report.scores[HASHED_BOW].ndcg


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate similar-company recommendations.")
    parser.add_argument("--output", type=Path, default=RESULTS_PATH)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    try:
        with db.conn() as connection:
            report = run(connection, seed=args.seed)
    finally:
        db.close_pool()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(report), encoding="utf-8")
    for title, table in (("all", report.scores), ("no twins", report.scores_without_twins)):
        for name, s in table.items():
            print(
                f"{title:<8} {name:<10} P@{K}={s.precision:.4f} NDCG@{K}={s.ndcg:.4f} "
                f"MRR={s.mrr:.4f} N={s.n}"
            )
    print(f"wrote {args.output}")
    if not passes_guard(report):
        bge, bow = report.scores[BGE_SMALL].ndcg, report.scores[HASHED_BOW].ndcg
        print(f"FAIL: {BGE_SMALL} NDCG@{K} {bge:.4f} <= {HASHED_BOW} {bow:.4f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
