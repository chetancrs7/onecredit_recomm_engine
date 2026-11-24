import hashlib
import math

import pytest

from company_recs import embedder
from company_recs.embedder import build_embed_text, description_hash


class TestBuildEmbedText:
    def test_uses_description_when_present(self) -> None:
        text = build_embed_text("Kopi Co", "Food & Beverage", "Selangor", "Operating coffee shops")
        assert text == "Operating coffee shops"

    @pytest.mark.parametrize("description", [None, "", "   ", "\t\n"])
    def test_falls_back_to_name_category_state(self, description: str | None) -> None:
        text = build_embed_text("Kopi Co", "Food & Beverage", "Selangor", description)
        assert text == "Kopi Co | Food & Beverage | Selangor"

    def test_fallback_without_state(self) -> None:
        assert (
            build_embed_text("Kopi Co", "Food & Beverage", None, None)
            == "Kopi Co | Food & Beverage"
        )

    def test_fallback_without_category_never_embeds_none(self) -> None:
        text = build_embed_text("Kopi Co", None, "Selangor", None)
        assert text == "Kopi Co | Selangor"
        assert "None" not in text

    def test_fallback_with_only_a_name(self) -> None:
        assert build_embed_text("Kopi Co", None, None, "") == "Kopi Co"

    def test_blank_category_and_state_are_skipped(self) -> None:
        assert build_embed_text("Kopi Co", "  ", "", None) == "Kopi Co"


class TestDescriptionHash:
    def test_is_hex_sha256_of_utf8(self) -> None:
        text = "Kedai kopi di Kuala Lumpur — café"
        assert description_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_is_stable_and_distinguishes_texts(self) -> None:
        assert description_hash("a") == description_hash("a")
        assert description_hash("a") != description_hash("b")
        assert len(description_hash("a")) == 64


def test_model_version_is_the_model_name() -> None:
    assert embedder.MODEL_VERSION == embedder.MODEL_NAME


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.mark.model
class TestEmbedding:
    def test_embed_one_is_384_dim_unit_vector(self) -> None:
        vector = embedder.embed_one("Operating cafes and coffee shops in the Klang Valley")
        assert len(vector) == embedder.EMBED_DIM
        assert math.isclose(math.hypot(*vector), 1.0, abs_tol=1e-6)

    def test_embed_batch_matches_embed_one(self) -> None:
        texts = ["Boutique hotel in Penang", "Production of rubber gloves for export"]
        batch = embedder.embed_batch(texts)
        assert len(batch) == 2
        for text, vector in zip(texts, batch, strict=True):
            assert vector == pytest.approx(embedder.embed_one(text), abs=1e-5)

    def test_embed_batch_of_nothing(self) -> None:
        assert embedder.embed_batch([]) == []

    def test_get_model_is_a_singleton(self) -> None:
        assert embedder.get_model() is embedder.get_model()

    def test_similar_descriptions_are_closer(self) -> None:
        cafe, coffee, gloves = embedder.embed_batch(
            [
                "Operating cafes and coffee shops in the Klang Valley",
                "Operating cafes and coffee shops in Johor Bahru",
                "Production of rubber gloves for export",
            ]
        )
        assert _cosine(cafe, coffee) > _cosine(cafe, gloves)
