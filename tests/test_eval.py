"""The eval: metric arithmetic, the baselines, and a full run against a real Postgres."""

import math
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from pgvector import Vector

from company_recs import db
from company_recs import eval as evaluation
from company_recs.embedder import EMBED_DIM
from company_recs.eval import (
    BGE_SMALL,
    HASHED_BOW,
    RANDOM,
    Company,
    Neighbour,
    dcg,
    hashed_bow_retriever,
    hashed_bow_vector,
    is_relevant,
    ndcg_at_k,
    precision_at_k,
    random_retriever,
    reciprocal_rank,
    same_text,
)


def _company(
    company_id: str, category: str | None = "Retail", description: str = "Retail of toys"
) -> Company:
    return Company(company_id, f"{company_id} Sdn. Bhd.", category, "Perak", description, True)


class TestMetrics:
    def test_precision_is_the_relevant_fraction(self) -> None:
        assert precision_at_k([True, False, True, False]) == 0.5
        assert precision_at_k([]) == 0.0

    def test_dcg_discounts_by_log2_of_rank_plus_one(self) -> None:
        assert dcg([True, True, False, True]) == pytest.approx(
            1 + 1 / math.log2(3) + 1 / math.log2(5)
        )

    def test_ndcg_is_one_for_an_ideal_ranking(self) -> None:
        assert ndcg_at_k([True] * 4, total_relevant=100) == pytest.approx(1.0)

    def test_ndcg_ideal_is_capped_by_the_relevant_companies_available(self) -> None:
        assert ndcg_at_k([True, False, False, False], total_relevant=1) == pytest.approx(1.0)
        assert ndcg_at_k([False, True, False, False], total_relevant=1) == pytest.approx(
            1 / math.log2(3)
        )

    def test_ndcg_of_a_partial_ranking(self) -> None:
        ideal = 1 + 1 / math.log2(3) + 1 / 2.0 + 1 / math.log2(5)
        assert ndcg_at_k([True, False, False, False], total_relevant=4) == pytest.approx(1 / ideal)

    def test_ndcg_is_zero_when_nothing_could_be_relevant(self) -> None:
        assert ndcg_at_k([False] * 4, total_relevant=0) == 0.0

    def test_reciprocal_rank_of_the_first_relevant(self) -> None:
        assert reciprocal_rank([False, False, True, True]) == pytest.approx(1 / 3)
        assert reciprocal_rank([False] * 4) == 0.0

    def test_relevance_is_an_exact_category_match(self) -> None:
        assert is_relevant(_company("A"), _company("B"))
        assert not is_relevant(_company("A"), _company("B", category="Logistics"))

    def test_no_category_is_never_relevant(self) -> None:
        assert not is_relevant(_company("A", category=None), _company("B", category=None))
        assert not is_relevant(_company("A"), _company("B", category=None))


class TestHashedBagOfWords:
    def test_vector_is_unit_length_and_384_dim(self) -> None:
        vector = hashed_bow_vector("Retail of baby products and toys")
        assert vector.shape == (EMBED_DIM,)
        assert np.linalg.norm(vector) == pytest.approx(1.0)

    def test_one_bucket_per_token_with_weight_one(self) -> None:
        vector = hashed_bow_vector("toys toys")
        assert np.count_nonzero(vector) == 1

    def test_tokens_are_lower_cased_and_split_on_whitespace(self) -> None:
        a = hashed_bow_vector("Retail of  TOYS")
        b = hashed_bow_vector("retail\tof toys")
        assert np.array_equal(a, b)

    def test_empty_text_is_the_zero_vector(self) -> None:
        assert not hashed_bow_vector("   ").any()

    def test_ranks_shared_words_first_and_excludes_self(self) -> None:
        query = _company("A", description="oil palm plantation")
        candidates = [
            query,
            _company("B", description="software consulting"),
            _company("C", description="oil palm plantation"),
            _company("D", description="palm oil mill"),
        ]
        ranked = [n.company.id for n in hashed_bow_retriever(candidates, k=3)(query)]
        assert ranked == ["C", "D", "B"]

    def test_skip_removes_exact_text_twins(self) -> None:
        query = _company("A", description="oil palm plantation")
        candidates = [
            query,
            _company("B", description="oil palm plantation"),
            _company("C", description="palm oil mill"),
        ]
        ranked = hashed_bow_retriever(candidates, k=4, skip=same_text)(query)
        assert [n.company.id for n in ranked] == ["C"]


class TestRandom:
    def test_never_returns_the_query_and_repeats_for_a_seed(self) -> None:
        candidates = [_company(f"C{i}") for i in range(6)]
        first = random_retriever(candidates, seed=1)
        second = random_retriever(candidates, seed=1)
        for query in candidates * 5:
            drawn = first(query)
            assert len(drawn) == 4
            assert query.id not in {n.company.id for n in drawn}
            assert drawn == second(query)


def test_evaluate_averages_over_queries() -> None:
    queries = [_company("A"), _company("B")]
    other = _company("X", category="Logistics")

    def retrieve(query: Company) -> list[Neighbour]:
        if query.id == "A":
            return [Neighbour(_company("B"), 1.0)] + [Neighbour(other, 0.5)] * 3
        return [Neighbour(other, 0.5)] * 4

    scores = evaluation.evaluate(queries, retrieve, lambda query: 1)
    assert scores.n == 2
    assert scores.precision == pytest.approx(0.125)
    assert scores.ndcg == pytest.approx(0.5)
    assert scores.mrr == pytest.approx(0.5)


def test_render_escapes_pipes_in_names() -> None:
    query = _company("A")
    report = evaluation.Report(
        candidates=2,
        queries=1,
        without_category=0,
        without_relevant=0,
        twin_queries=0,
        seed=1,
        scores={RANDOM: evaluation.Scores(0.25, 0.25, 0.25, 1)},
        scores_without_twins={RANDOM: evaluation.Scores(0.0, 0.0, 0.0, 1)},
        samples=[(query, [Neighbour(_company("B | C"), 0.9)])],
        missing_samples=["SYN-999999"],
    )
    markdown = evaluation.render(report)
    assert "| random | 0.2500 | 0.2500 | 0.2500 | 1 |" in markdown
    assert "B \\| C Sdn. Bhd." in markdown
    assert "SYN-999999" in markdown


CATEGORIES = ("Agriculture", "Technology")


def _axis(category: str, number: int) -> Vector:
    """Close to the category's own axis, so bge-small neighbours share the category."""
    values = [0.0] * EMBED_DIM
    values[CATEGORIES.index(category)] = 1.0
    values[10 + number] = 0.3
    return Vector(values)


def _scattered(number: int) -> Vector:
    values = np.random.default_rng(number).standard_normal(EMBED_DIM)
    return Vector((values / np.linalg.norm(values)).tolist())


def _seed(*, embeddings_follow_category: bool) -> None:
    """Twelve companies in two categories, where exactly one retriever can see the category.

    With `embeddings_follow_category`, each embedding sits near its category's axis and every
    description is the same words plus one unique token, so only bge-small can tell the
    categories apart. Otherwise the embeddings are scattered and each category has its own
    words, so only the bag of words can.
    """
    words = {"Agriculture": "planting harvesting crops", "Technology": "coding testing software"}
    with db.conn() as connection:
        for number in range(12):
            category = CATEGORIES[number % 2]
            if embeddings_follow_category:
                vector, shared = _axis(category, number), "services across a wide range"
            else:
                vector, shared = _scattered(number), words[category]
            connection.execute(
                """
                INSERT INTO companies (id, name, category, state, description, embedding,
                                       source_updated_at, source_message_id)
                VALUES (%s, %s, %s, 'Perak', %s, %s, now(), 'test')
                """,
                (
                    f"SYN-{number:06d}",
                    f"Company {number}",
                    category,
                    f"word{number} {shared}",
                    vector,
                ),
            )


@pytest.mark.integration
class TestRun:
    def test_writes_results_and_passes_when_embeddings_beat_words(
        self, clean_db: str, tmp_path: Path
    ) -> None:
        _seed(embeddings_follow_category=True)
        output = tmp_path / "results.md"
        assert evaluation.main(["--output", str(output)]) == 0

        markdown = output.read_text(encoding="utf-8")
        assert "| Retriever | P@4 | NDCG@4 | MRR | N |" in markdown
        assert "| bge-small | 1.0000 | 1.0000 | 1.0000 | 12 |" in markdown
        assert "### SYN-000010 · Company 10" in markdown
        assert "SYN-000024, SYN-000025, SYN-000012, SYN-000017" in markdown

    def test_exits_nonzero_when_bge_small_does_not_beat_hashed_bow(
        self, clean_db: str, tmp_path: Path
    ) -> None:
        _seed(embeddings_follow_category=False)
        with db.conn() as connection:
            report = evaluation.run(connection)
        assert report.scores[BGE_SMALL].ndcg <= report.scores[HASHED_BOW].ndcg
        assert evaluation.main(["--output", str(tmp_path / "results.md")]) == 1

    def test_queries_skip_short_descriptions_missing_categories_and_struck_off(
        self, clean_db: str
    ) -> None:
        _seed(embeddings_follow_category=True)
        with db.conn() as connection:
            connection.execute("UPDATE companies SET description = 'Farm' WHERE id = 'SYN-000000'")
            connection.execute("UPDATE companies SET category = NULL WHERE id = 'SYN-000001'")
            connection.execute("UPDATE companies SET is_active = false WHERE id = 'SYN-000002'")
            report = evaluation.run(connection)
        assert report.candidates == 11
        assert report.queries == 9
        assert report.without_category == 1
        assert Counter(score.n for score in report.scores.values()) == {9: 3}
