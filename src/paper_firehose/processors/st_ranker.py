"""
Sentence-Transformers based ranking processor.

Minimal implementation: computes cosine similarity between a topic query
and entry texts, and returns scores suitable for writing into papers.db
(`rank_score`).

This module is intentionally lean and resilient: if sentence-transformers
is not available or the model cannot be loaded, it logs and returns an
empty result so callers can decide how to proceed.
"""

from __future__ import annotations

# Set before any heavy imports to silence HF tokenizers warning.
import os as _os
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import logging
from typing import Iterable, List, Tuple, Optional

logger = logging.getLogger(__name__)


class STRanker:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        """Lazy-load a SentenceTransformer model, logging a warning on failure."""
        self.model_name = model_name
        self._model = None
        self._util = None
        try:
            from sentence_transformers import SentenceTransformer, util  # type: ignore
            self._model = SentenceTransformer(model_name)
            self._util = util
        except Exception as e:  # pragma: no cover - optional dependency
            logger.warning(
                "sentence-transformers unavailable or model load failed (%s). Ranking will be skipped.",
                e,
            )

    def available(self) -> bool:
        """Return True when the embedding model loaded successfully."""
        return self._model is not None and self._util is not None

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
        if not self.available():  # graceful no-op
            return []

        model = self._model
        util = self._util
        assert model is not None and util is not None

        # Prepare batch
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

        q_emb = model.encode([query.strip()], normalize_embeddings=True)
        d_emb = model.encode(docs, normalize_embeddings=True)
        sims = util.cos_sim(q_emb, d_emb).tolist()[0]

        return list(zip(ids, topics, sims))
