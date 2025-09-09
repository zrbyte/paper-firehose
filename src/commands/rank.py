"""
Rank command: compute and write rank scores into papers.db (rank_score).

Initial minimal version:
- Reads per-topic ranking config (query, model)
- Fetches entries with status='filtered' for the topic(s)
- Computes cosine similarity (Sentence-Transformers) between query and title
- Writes scores to `rank_score` (no status change)

Notes:
- If sentence-transformers is unavailable or model download fails, the command logs
  and skips scoring without raising.
"""

from __future__ import annotations

# Set before any heavy imports to silence HF tokenizers warning.
import os as _os
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import logging
from typing import Optional, List, Dict, Any

from core.config import ConfigManager
from core.database import DatabaseManager
from processors.st_ranker import STRanker

logger = logging.getLogger(__name__)


def _build_entry_text(entry: Dict[str, Any]) -> str:
    """Return the text to be ranked for an entry (title-only for now)."""
    # Keep minimal as requested; can switch to title+summary later
    return (entry.get("title") or "").strip()


def run(config_path: str, topic: Optional[str] = None) -> None:
    """
    Compute rank scores and write them into papers.db (rank_score).

    Args:
        config_path: Path to main config
        topic: Optional topic name; if None, process all topics
    """
    logger.info("Starting rank command (write scores only)")

    cfg_mgr = ConfigManager(config_path)
    if not cfg_mgr.validate_config():
        logger.error("Configuration validation failed")
        return

    config = cfg_mgr.load_config()
    db = DatabaseManager(config)

    topics: List[str]
    if topic:
        topics = [topic]
    else:
        topics = cfg_mgr.get_available_topics()

    for topic_name in topics:
        try:
            tcfg = cfg_mgr.load_topic_config(topic_name)
        except Exception as e:
            logger.error("Failed to load topic '%s': %s", topic_name, e)
            continue

        ranking_cfg = (tcfg.get("ranking") or {}) if isinstance(tcfg, dict) else {}
        query = ranking_cfg.get("query") or ""
        model_name = ranking_cfg.get("model") or "all-MiniLM-L6-v2"

        if not query:
            logger.warning("Topic '%s' has no ranking.query; skipping.", topic_name)
            continue

        # Load candidate entries from papers.db
        entries = db.get_current_entries(topic=topic_name, status="filtered")
        if not entries:
            logger.info("No filtered entries for topic '%s'", topic_name)
            continue

        # Prepare ranker
        ranker = STRanker(model_name=model_name)
        if not ranker.available():
            logger.warning("Ranker unavailable for topic '%s'; skipping.", topic_name)
            continue

        # Build batch (id, topic, text)
        batch = [(e["id"], e["topic"], _build_entry_text(e)) for e in entries]
        scores = ranker.score_entries(query, batch)

        # Write scores
        updated = 0
        for eid, tname, score in scores:
            try:
                db.update_entry_rank(eid, tname, float(score))
                updated += 1
            except Exception as e:
                logger.error("Failed to update rank for %s/%s: %s", eid[:8], tname, e)

        logger.info("Topic '%s': wrote rank_score for %d entries", topic_name, updated)

    db.close_all_connections()
    logger.info("Rank command completed")
