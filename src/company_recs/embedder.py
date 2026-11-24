"""Text embeddings for company descriptions, computed locally on CPU with fastembed."""

import hashlib
import threading
from collections.abc import Sequence
from typing import Final

import numpy as np
from fastembed import TextEmbedding

from company_recs.config import get_settings

MODEL_NAME: Final = get_settings().embed_model
MODEL_VERSION: Final = MODEL_NAME
EMBED_DIM: Final = 384
BATCH_SIZE: Final = 4

_model: TextEmbedding | None = None
_model_lock = threading.Lock()


def get_model() -> TextEmbedding:
    """Return the shared embedding model, loading it on first use rather than on import."""
    global _model
    with _model_lock:
        if _model is None:
            _model = TextEmbedding(model_name=MODEL_NAME)
        return _model


def build_embed_text(
    name: str, category: str | None, state: str | None, description: str | None
) -> str:
    """Return the text to embed for a company.

    Implements the "no lost companies" rule for empty descriptions: a company whose description is
    missing, empty or only whitespace is still embedded, from `name | category | state`, so it can
    appear in other companies' recommendations. Missing parts are left out rather than written as
    "None", so the fallback is `name | state` when the category is missing too.
    """
    if description is not None and description.strip():
        return description
    parts = [name, category, state]
    return " | ".join(part.strip() for part in parts if part is not None and part.strip())


def _to_unit_vector(vector: np.ndarray) -> list[float]:
    values = np.asarray(vector, dtype=np.float64)
    if values.shape != (EMBED_DIM,):
        raise ValueError(
            f"expected a {EMBED_DIM}-dim embedding from {MODEL_NAME}, got {values.shape}"
        )
    norm = float(np.linalg.norm(values))
    if norm == 0.0:
        raise ValueError("embedding has zero length and cannot be normalised")
    return [float(x) for x in values / norm]


def embed_batch(texts: Sequence[str]) -> list[list[float]]:
    """Embed many texts in one call to the model's batch API. Each vector is 384-dim, length 1."""
    if not texts:
        return []
    return [
        _to_unit_vector(vector) for vector in get_model().embed(list(texts), batch_size=BATCH_SIZE)
    ]


def embed_one(text: str) -> list[float]:
    """Embed a single text. The vector is 384-dim, length 1."""
    return embed_batch([text])[0]


def description_hash(text: str) -> str:
    """Return the hex SHA-256 of the UTF-8 text, used to skip re-embedding unchanged text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
