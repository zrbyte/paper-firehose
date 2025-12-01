"""
Fetch abstracts and populate both papers.db (entries.abstract) and
matched_entries_history.db (matched_entries.abstract).

Rules
-----

- First pass fills arXiv/cond-mat abstracts from summary (no threshold).
- Then for rows with ``rank_score >= threshold``: Crossref (DOI, then title search),
  followed by aggregator fallbacks (Semantic Scholar, OpenAlex, PubMed).
- Only process topics where the topic YAML has ``abstract_fetch.enabled: true``.
- Use per-topic ``abstract_fetch.rank_threshold`` if set; otherwise fall back to
  global ``defaults.rank_threshold`` in ``config.yaml``.
- Respect API rate limits; include a descriptive ``User-Agent`` with contact email
  and obey ``Retry-After`` on 429/503 responses. Default to ~1 request/second.
"""

from __future__ import annotations

import os
from typing import Optional
import logging

import requests

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.command_utils import resolve_topics
from ..processors.abstract_fetcher import (
    fill_arxiv_summaries,
    crossref_pass,
    fallback_pass,
)


def run(
    config_path: str,
    topic: Optional[str] = None,
    *,
    mailto: Optional[str] = None,
    max_per_topic: Optional[int] = None,
    rps: float = 1.0
) -> None:
    """Fetch and write abstracts into papers.db for ranked entries.

    Args:
        config_path: Path to the main configuration file (defaults to ~/.paper_firehose/config.yaml)
        topic: Optional single topic; otherwise process all topics
        mailto: Contact email for Crossref User-Agent
        max_per_topic: Optional cap on number of fetches per topic
        rps: Requests per second throttle (default ~1 req/s)
    """
    logger = logging.getLogger(__name__)
    cfg = ConfigManager(config_path)
    config = cfg.load_config()
    db = DatabaseManager(config)

    topics = resolve_topics(cfg, topic)
    # Default threshold
    defaults = (config.get('defaults') or {})
    global_thresh = float(defaults.get('rank_threshold', 0.35))
    abs_defaults = (defaults.get('abstracts') or {})

    # Resolve contact email: CLI arg -> MAILTO env -> config.defaults.abstracts.mailto -> fallback
    mailto = mailto or os.environ.get("MAILTO") or abs_defaults.get('mailto') or "nemesp@gmail.com"

    # RPS from config if provided
    if rps == 1.0:  # only use config if user didn't override
        try:
            rps_cfg = float(abs_defaults.get('rps')) if abs_defaults.get('rps') is not None else None
            if rps_cfg and rps_cfg > 0:
                rps = rps_cfg
        except Exception:
            pass
    max_retries = int(abs_defaults.get('max_retries', 3))

    sess = requests.Session()
    min_interval = 1.0 / max(rps, 0.01)

    # Step 1: First pass — fill arXiv/cond-mat abstracts from summaries (no threshold)
    filled = fill_arxiv_summaries(db, topics)
    logger.info(f"Abstracts: arXiv/cond-mat summary fill updated={filled}")

    for t in topics:
        tcfg = cfg.load_topic_config(t)
        af_cfg = tcfg.get('abstract_fetch') or {}
        if not af_cfg or not af_cfg.get('enabled', False):
            logger.info(f"Abstract fetch disabled for topic '{t}', skipping")
            continue
        thr = float(af_cfg.get('rank_threshold', global_thresh))

        # Step 2: Crossref-only pass for above-threshold entries
        try:
            fetched_crossref = crossref_pass(
                db, t, thr,
                mailto=mailto,
                session=sess,
                min_interval=min_interval,
                max_per_topic=max_per_topic,
                max_retries=max_retries,
            )
        except Exception as e:
            logger.error(f"Crossref pass failed for topic '{t}': {e}. Continuing with fallback providers.")
            fetched_crossref = 0
        # Step 3: Fallback APIs for remaining above-threshold entries
        try:
            fetched_fallback = fallback_pass(
                db, t, thr,
                mailto=mailto,
                session=sess,
                min_interval=min_interval,
                max_per_topic=max_per_topic
            )
        except Exception as e:
            logger.error(f"Fallback providers pass failed for topic '{t}': {e}")
            fetched_fallback = 0
        logger.info(f"Abstracts: topic='{t}' threshold={thr} updated_crossref={fetched_crossref} updated_fallback={fetched_fallback}")

        # HTML generation is handled by the `html` command.

    # no return
