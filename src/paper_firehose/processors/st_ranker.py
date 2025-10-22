"""FastEmbed based ranking processor.

Minimal implementation: computes cosine similarity between a topic query
and entry texts, and returns scores suitable for writing into ``papers.db``
(``rank_score``).

This module is intentionally lean and resilient: if FastEmbed (or the
underlying model download) is unavailable, it first attempts to fall back to a
``sentence_transformers`` model.  If neither backend can be loaded we log and
return an empty result so callers can decide how to proceed.

The previous :mod:`sentence_transformers`-powered implementation carried a
significant PyTorch dependency.  The new design keeps the same public API but
leans on the much lighter ``fastembed`` runtime.  To make future refactors
easier, the code includes generous inline comments that describe the control
flow and the data transformations performed before we score entries.
"""

from __future__ import annotations

# Set before any heavy imports to silence HF tokenizers warning.
import os as _os
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import logging
from typing import Iterable, List, Tuple, Optional, Any

import numpy as np

logger = logging.getLogger(__name__)


_MODEL_ALIASES = {
    # Historical defaults from the SentenceTransformers era.  Keeping them in
    # place means topic configs (and tests) can continue to reference the old
    # names without noticing the backend swap.  Each entry maps a legacy
    # Sentence-Transformers identifier to the FastEmbed model we now ship with.
    # ``STRanker`` always stores the original request (``model_name``) so that
    # logs reflect the user intent even when the resolved backend differs.
    "all-MiniLM-L6-v2": "BAAI/bge-small-en-v1.5",
    "sentence-transformers/all-MiniLM-L6-v2": "BAAI/bge-small-en-v1.5",
}


def _load_text_embedding(model_name: str):
    """Return a FastEmbed ``TextEmbedding`` instance for the given model."""
    from fastembed import TextEmbedding  # type: ignore

    return TextEmbedding(model_name=model_name)


class _SentenceTransformerAdapter:
    """Thin wrapper that mimics the ``TextEmbedding`` interface."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(model_name)

    def embed(self, documents, **_kwargs):
        if isinstance(documents, str):
            docs = [documents]
        else:
            docs = list(documents)
        if not docs:
            return []

        vectors = self._model.encode(
            docs,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        )
        if isinstance(vectors, np.ndarray):
            if vectors.ndim == 1:
                return [np.asarray(vectors, dtype=np.float32)]
            return [np.asarray(vec, dtype=np.float32) for vec in vectors]
        return [np.asarray(vec, dtype=np.float32) for vec in vectors]


def _load_sentence_transformer(model_name: str):
    """Return a SentenceTransformer-backed adapter."""

    return _SentenceTransformerAdapter(model_name)


class STRanker:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """Lazy-load a FastEmbed text embedding model, logging a warning on failure."""

        # ``model_name`` reflects whatever the caller configured in their topic
        # YAML.  We translate it through ``_MODEL_ALIASES`` so legacy configs keep
        # functioning even though the backend swapped out from
        # ``SentenceTransformer`` to FastEmbed.
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
                "FastEmbed unavailable or model load failed (%s). Attempting SentenceTransformer fallback.",
                e,
            )
            try:
                self._model = _load_sentence_transformer(self.model_name)
                self.backend = "sentence-transformers"
                logger.info(
                    "Using SentenceTransformer fallback for model '%s'", self.model_name
                )
            except Exception as fallback_err:  # pragma: no cover - optional dependency
                logger.warning(
                    "SentenceTransformer fallback unavailable (%s). Ranking will be skipped.",
                    fallback_err,
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
            # ``entries`` can be any iterable (including generators), therefore
            # we eagerly consume it into simple Python lists.  Downstream numpy
            # operations expect random-access sequences, so materialising here
            # keeps the rest of the code straightforward.
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
        # because of the prior normalisation.  This mirrors the behaviour of the
        # old Sentence-Transformers implementation where ``cos_sim(query, docs)``
        # accomplished the same thing via PyTorch.
        sims = doc_array @ q_vec

        # Re-unify the metadata with the computed scores.  The lists are kept in
        # lockstep so ``zip`` is safe and stable with respect to the input order.
        return list(zip(ids, topics, sims.tolist()))
