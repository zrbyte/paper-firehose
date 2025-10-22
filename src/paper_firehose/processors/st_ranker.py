"""FastEmbed-based ranking processor.

Minimal implementation: computes cosine similarity between a topic query
and entry texts, and returns scores suitable for writing into ``papers.db``
(``rank_score``).

The previous :mod:`sentence_transformers`-powered implementation carried a
significant PyTorch dependency.  The new design keeps the same public API but
leans entirely on the lighter ``fastembed`` runtime.  If the configured model
cannot be loaded we log the failure and return an empty result so callers can
decide how to proceed.
"""

from __future__ import annotations

# Set before any heavy imports to silence HF tokenizers warning.
import os as _os
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import logging
from typing import Iterable, List, Tuple, Optional, Any

import numpy as np

logger = logging.getLogger(__name__)


_MODEL_ALIASES: dict[str, str] = {}


def _load_text_embedding(model_name: str):
    """Return a FastEmbed ``TextEmbedding`` instance for the given model."""
    from fastembed import TextEmbedding  # type: ignore

    return TextEmbedding(model_name=model_name)


class STRanker:
    def __init__(self, model_name: str = "BAAI/bge-base-en-v1.5") -> None:
        """Lazy-load a FastEmbed text embedding model, logging a warning on failure."""

        # ``model_name`` reflects whatever the caller configured in their topic
        # YAML.  We translate it through ``_MODEL_ALIASES`` so future remappings
        # can be handled centrally; currently this is an identity mapping.
        resolved_name = _MODEL_ALIASES.get(model_name, model_name)
        # ``model_name`` captures the config value verbatim; ``_resolved_name``
        # is the concrete FastEmbed identifier we will attempt to load.  Keeping
        # both around helps debug user reports like "my alias stopped working"
        # because we can emit both names in logs.
        self.model_name = model_name
        self._resolved_name = resolved_name
        self._model: Optional[Any] = None
        self.backend: Optional[str] = None
        try:
            # FastEmbed lazily downloads the model weights (if necessary) when the
            # object is instantiated.  We keep the construction inside a ``try`` so
            # environments without network access degrade gracefully instead of
            # crashing the command.
            self._model = _load_text_embedding(resolved_name)
            self.backend = "fastembed"
        except Exception as e:  # pragma: no cover - optional dependency
            logger.warning(
                "FastEmbed model '%s' unavailable: %s. Ranking will be skipped.",
                resolved_name,
                e,
            )

    def available(self) -> bool:
        """Return True when the embedding model loaded successfully."""
        return self._model is not None

    def score_entries(
        self,
        query: str,
        entries: Iterable[Tuple[str, str, str]],
        *,
        use_summary: bool = False,
    ) -> List[Tuple[str, str, float]]:
        """Compute similarity scores for entries.

        Args:
            query: Natural-language ranking query
            entries: Iterable of (entry_id, topic, text) where text is typically the title
            use_summary: If True, the provided text should include summary; default False

        Returns:
            List of (entry_id, topic, score) tuples
        """
        if not self.available():  # graceful no-op when the backend could not load
            return []

        model = self._model
        assert model is not None

        # Prepare batch
        # -------------
        # The ``rank`` command hands us an iterable of (entry_id, topic, text).
        # We split those tuples into parallel arrays because FastEmbed's API
        # accepts a simple sequence of strings.  Keeping the metadata alongside
        # the text lets us reassemble the results when we emit scores.
        ids: List[str] = []
        topics: List[str] = []
        docs: List[str] = []
        for eid, topic, text in entries:
            ids.append(eid)
            topics.append(topic)
            # Be conservative: strip/normalize; title is usually enough
            docs.append((text or "").strip())

        if not docs:
            return []

        try:
            # FastEmbed returns generators; ``list`` consumption keeps the logic
            # close to the former ``SentenceTransformer.encode`` usage.  Each item
            # is a dense embedding vector.
            q_vecs = list(model.embed([query.strip()]))
            d_vecs = list(model.embed(docs))
        except Exception as e:  # pragma: no cover - backend failure
            logger.warning("FastEmbed inference failed (%s). Ranking will be skipped.", e)
            return []

        if not q_vecs or not d_vecs:
            return []

        # Normalise vectors
        # -----------------
        # Cosine similarity is sensitive to vector magnitude.  The
        # SentenceTransformers version handled this internally, so we replicate
        # the behaviour by normalising each embedding to unit length before we
        # project the query against the document matrix.
        q_vec = np.asarray(q_vecs[0], dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        doc_matrix = []
        for vec in d_vecs:
            arr = np.asarray(vec, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            doc_matrix.append(arr)

        if not doc_matrix:
            return []

        # ``doc_matrix`` is now a list of unit vectors.  Stacking them provides an
        # ``n x d`` array where ``n`` equals the number of candidate entries.
        doc_array = np.vstack(doc_matrix)
        # ``@`` performs a dense matrix/vector multiply that yields cosine scores
        # because of the prior normalisation.
        sims = doc_array @ q_vec

        return list(zip(ids, topics, sims.tolist()))
