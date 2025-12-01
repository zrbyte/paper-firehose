"""
Rank command: compute and write rank scores into papers.db (``rank_score``).

Initial minimal version
-----------------------

- Read per-topic ranking config (query, model).
- Fetch entries with ``status='filtered'`` for the topic(s).
- Compute cosine similarity (Sentence-Transformers) between query and title.
- Write scores to ``rank_score`` (no status change).

Notes
-----

- If Sentence-Transformers is unavailable or model download fails, the command logs
  and skips scoring without raising.
"""

from __future__ import annotations

# Set before any heavy imports to silence HF tokenizers warning.
import os as _os
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import logging
from typing import Optional, List, Dict, Any
import unicodedata
import re

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.command_utils import resolve_topics
from ..core.text_utils import strip_accents, normalize_name, parse_name_parts, names_match
from ..core.model_manager import ensure_local_model
from ..processors.st_ranker import STRanker

logger = logging.getLogger(__name__)


def _build_entry_text(entry: Dict[str, Any]) -> str:
    """Return the text to be ranked for an entry (title-only for now)."""
    # Keep minimal as requested; can switch to title+summary later
    return (entry.get("title") or "").strip()


def _entry_has_preferred_author(entry: Dict[str, Any], preferred_authors: List[str]) -> bool:
    """Return True when entry authors overlap with the preferred author patterns."""
    if not preferred_authors:
        return False
    authors_blob = entry.get("authors") or ""
    parts = re.split(r"[,;]", authors_blob)
    authors = [p.strip() for p in parts if p.strip()]
    if not authors:
        return False
    for want in preferred_authors:
        for have in authors:
            if names_match(have, want):
                return True
    return False


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

    topics = resolve_topics(cfg_mgr, topic)

    for topic_name in topics:
        try:
            tcfg = cfg_mgr.load_topic_config(topic_name)
        except Exception as e:
            logger.error("Failed to load topic '%s': %s", topic_name, e)
            continue

        ranking_cfg = (tcfg.get("ranking") or {}) if isinstance(tcfg, dict) else {}
        query = ranking_cfg.get("query") or ""
        model_spec = ranking_cfg.get("model") or "all-MiniLM-L6-v2"
        # Ensure local vendored model (best-effort); falls back to spec on failure
        model_name = ensure_local_model(model_spec)
        if model_name != model_spec:
            logger.info("Topic '%s': using local model at %s", topic_name, model_name)
        negative_terms = [
            t.strip() for t in (ranking_cfg.get("negative_queries") or []) if isinstance(t, str) and t.strip()
        ]
        preferred_authors = [
            t.strip() for t in (ranking_cfg.get("preferred_authors") or []) if isinstance(t, str) and t.strip()
        ]
        author_boost = float(ranking_cfg.get("priority_author_boost") or 0.0)

        # Global priority journal boost
        prio_keys = set(config.get("priority_journals", []) or [])
        feeds_cfg = (config.get("feeds") or {})
        prio_display_names = set()
        for k in prio_keys:
            feed = feeds_cfg.get(k)
            if isinstance(feed, dict):
                name = feed.get("name")
                if name:
                    prio_display_names.add(str(name))
        journal_boost = float(config.get("priority_journal_boost") or 0.0)

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

        # Apply simple downweight for entries containing any negative term in title or summary
        if negative_terms:
            neg_set = {t.lower() for t in negative_terms}
            # Build quick lookup from (id, topic) -> entry for text access
            entry_by_key = {(e["id"], e["topic"]): e for e in entries}
            adjusted: list[tuple[str, str, float]] = []
            penalized = 0
            # Negative penalty configurable: topic.ranking.negative_penalty or defaults.ranking_negative_penalty (global), default 0.25
            global_neg_pen = float((config.get("defaults") or {}).get("ranking_negative_penalty", 0.25))
            neg_penalty = float(ranking_cfg.get("negative_penalty", global_neg_pen))
            for eid, tname, score in scores:
                entry = entry_by_key.get((eid, tname)) or {}
                title = (entry.get("title") or "").lower()
                summary = (entry.get("summary") or "").lower()
                blob = f"{title} {summary}"
                has_negative = any(term in blob for term in neg_set)
                if has_negative:
                    # Subtract a configurable penalty and clamp to [0, 1]
                    new_score = max(0.0, float(score) - neg_penalty)
                    penalized += 1
                else:
                    new_score = float(score)
                adjusted.append((eid, tname, new_score))
            logger.info(
                "Topic '%s': applied negative term penalty to %d entries", topic_name, penalized
            )
            scores = adjusted

        # Write scores with boosts
        updated = 0
        boosted_auth = 0
        boosted_jour = 0
        entry_by_key = {(e["id"], e["topic"]): e for e in entries}
        for eid, tname, score in scores:
            s = float(score)
            entry = entry_by_key.get((eid, tname)) or {}
            # Preferred author boost
            if preferred_authors and author_boost > 0 and _entry_has_preferred_author(entry, preferred_authors):
                s += author_boost
                boosted_auth += 1
            # Priority journal boost by display name
            if journal_boost > 0:
                feed_name = (entry.get("feed_name") or "").strip()
                if feed_name in prio_display_names:
                    s += journal_boost
                    boosted_jour += 1
            s = max(0.0, min(1.0, s))
            try:
                db.update_entry_rank(eid, tname, s)
                try:
                    db.update_history_rank(eid, s)
                except Exception as history_err:
                    logger.debug(
                        "Topic '%s': failed to persist rank_score to history for %s: %s",
                        tname,
                        eid[:8],
                        history_err,
                    )
                updated += 1
            except Exception as e:
                logger.error("Failed to update rank for %s/%s: %s", eid[:8], tname, e)

        if preferred_authors and author_boost > 0:
            logger.info(
                "Topic '%s': applied preferred author boost to %d entries (+%.2f)",
                topic_name,
                boosted_auth,
                author_boost,
            )
        if journal_boost > 0:
            logger.info(
                "Topic '%s': applied priority journal boost to %d entries (+%.2f)",
                topic_name,
                boosted_jour,
                journal_boost,
            )
        logger.info("Topic '%s': wrote rank_score for %d entries", topic_name, updated)

        # HTML generation moved to the standalone `html` command.

    db.close_all_connections()
    logger.info("Rank command completed")
