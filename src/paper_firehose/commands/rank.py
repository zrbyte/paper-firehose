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
import unicodedata
import re

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.paths import get_system_path, resolve_data_dir
from ..processors.st_ranker import STRanker

logger = logging.getLogger(__name__)


def _has_model_files(path: str) -> bool:
    """Heuristic check that a local Sentence-Transformers model folder is valid."""
    from pathlib import Path
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    # Common files for ST models
    candidates = [p / "config.json", p / "modules.json"]
    return any(c.exists() for c in candidates)


def _ensure_local_model(model_spec: str) -> str:
    """Ensure a local model directory exists for the given spec and return the path or original spec.

    Behavior:
    - If spec is the default alias 'all-MiniLM-L6-v2':
        Use 'models/all-MiniLM-L6-v2'. If missing or empty, download
        'sentence-transformers/all-MiniLM-L6-v2' into that folder.
    - If spec looks like a repo id (e.g., 'sentence-transformers/x' or 'intfloat/e5-small'):
        Vendor to 'models/<last-segment>' when not present or empty.
    - If spec is a local path and valid, return it. If it exists but appears empty,
      try to infer repo id from the folder name and download into it.
    - On any failure (e.g., no network), return the original spec and let STRanker handle it.
    """
    from pathlib import Path
    import re
    import shutil

    # Try local path directly if it's already valid
    if Path(model_spec).exists() and _has_model_files(model_spec):
        return model_spec

    models_root = resolve_data_dir('models', ensure_exists=True)
    system_models_root = get_system_path('models')

    repo_id: str | None = None
    target_dir: Path | None = None

    # Case 1: default alias
    if model_spec == "all-MiniLM-L6-v2":
        repo_id = "sentence-transformers/all-MiniLM-L6-v2"
        target_dir = models_root / "all-MiniLM-L6-v2"

    # Case 2: looks like HF repo id "org/name"
    elif "/" in model_spec and not Path(model_spec).exists():
        repo_id = model_spec
        last = model_spec.rsplit("/", 1)[-1]
        # sanitize last segment for filesystem safety just in case
        last = re.sub(r"[^A-Za-z0-9._\-]", "_", last)
        target_dir = models_root / last

    # Case 3: non-default spec that may be a local folder name or alias
    else:
        # If spec is a path but empty, try infer repo as sentence-transformers/<name>
        p = Path(model_spec)
        name = p.name if p.name else str(model_spec)
        repo_id = f"sentence-transformers/{name}"
        target_dir = p if p.is_absolute() else models_root / name

    assert target_dir is not None and repo_id is not None

    # If the target already looks valid, use it
    if _has_model_files(str(target_dir)):
        return str(target_dir)

    # If the system bundle ships the model, copy it into the runtime directory
    if system_models_root.exists():
        system_candidate = system_models_root / target_dir.name
        try:
            if system_candidate.exists() and system_candidate.resolve() != target_dir.resolve():
                shutil.copytree(system_candidate, target_dir)
                if _has_model_files(str(target_dir)):
                    return str(target_dir)
        except FileExistsError:
            pass
        except Exception as e:
            logger.debug("Model seed copy failed for %s -> %s: %s", system_candidate, target_dir, e)

    # Attempt download (best-effort)
    try:
        from huggingface_hub import snapshot_download  # type: ignore
        target_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(target_dir),
            local_dir_use_symlinks=False,
        )
        return str(target_dir)
    except Exception as e:  # pragma: no cover - network optional
        logger.warning("Model vendor failed for '%s' -> %s: %s", repo_id, target_dir, e)
        # Fall back to original spec; STRanker will try to resolve
        return model_spec


def _build_entry_text(entry: Dict[str, Any]) -> str:
    """Return the text to be ranked for an entry (title-only for now)."""
    # Keep minimal as requested; can switch to title+summary later
    return (entry.get("title") or "").strip()


def _strip_accents(text: str) -> str:
    """Return ASCII-ish text by removing accent marks via Unicode normalization."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _norm_name(text: str) -> str:
    """Normalize a human name for loose matching (strip accents/punctuation/case)."""
    t = _strip_accents(text or "").lower()
    t = re.sub(r"[^a-z\s\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _parse_name_parts(name: str) -> tuple[str, List[str]]:
    """Return (lastname, initials[]) from a human name.

    Handles "Last, First M" and "First M Last" styles, ignores accents/case.
    """
    if not name:
        return "", []
    # Preserve comma pattern before normalization for ordering hint
    if "," in name:
        last_raw, _, rest_raw = name.partition(",")
        last = _norm_name(last_raw)
        rest = _norm_name(rest_raw)
        tokens = rest.split()
    else:
        n = _norm_name(name)
        tokens = n.split()
        last = tokens[-1] if tokens else ""
        tokens = tokens[:-1]
    initials = [t[0] for t in tokens if t]
    return last, initials


def _names_match(a: str, b: str) -> bool:
    """Heuristic author-name comparator supporting initials and comma forms."""
    la, ia = _parse_name_parts(a)
    lb, ib = _parse_name_parts(b)
    if not la or not lb:
        return False
    if la != lb:
        return False
    if ia and ib and not set(ia).intersection(ib):
        return False
    return True


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
            if _names_match(have, want):
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
        model_spec = ranking_cfg.get("model") or "all-MiniLM-L6-v2"
        # Ensure local vendored model (best-effort); falls back to spec on failure
        model_name = _ensure_local_model(model_spec)
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
