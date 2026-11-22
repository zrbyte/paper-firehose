"""
Synthesize command: one cross-paper synthesis per topic.

Reads the papers a topic matched in the last N days from the history database
(matched_entries_history.db), asks an OpenAI model what they have in common —
an overview and a few themes, each naming its papers — and stores the answer in
the history database (table ``syntheses``). The ``html`` and ``email`` commands
show the latest stored synthesis above a topic's papers.

Needs ``OPENAI_API_KEY`` in the environment (one paid call per topic), except
with ``dry_run``.

Topic configuration (all keys optional)::

    synthesis:
      enabled: true          # include this topic when no --topic is given
      days: 7                # papers matched in the last N days
      max_papers: 40         # at most this many papers (best rank score first)
      min_rank: 0.3          # only papers with at least this score
      model: "gpt-6.2-sol"
      reasoning_effort: "low"
      prompt: |
        Write for an expert reader interested in {ranking_query}.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

from ..core.command_utils import resolve_topics
from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..processors import synthesizer as synth

logger = logging.getLogger(__name__)


def _settings(tcfg: Dict[str, Any], days: Optional[int], limit: Optional[int],
              min_rank: Optional[float]) -> Dict[str, Any]:
    """Merge the topic's ``synthesis:`` section with the command-line values (which win)."""
    section = tcfg.get("synthesis") or {}
    if not isinstance(section, dict):
        raise ValueError("'synthesis' must be a mapping")

    def number(value: Any, name: str, kind=int):
        try:
            return kind(value)
        except (TypeError, ValueError):
            raise ValueError(f"synthesis.{name} must be a number") from None

    resolved_days = number(days if days is not None else section.get("days", synth.DEFAULT_DAYS), "days")
    resolved_limit = number(
        limit if limit is not None else section.get("max_papers", synth.DEFAULT_MAX_PAPERS), "max_papers"
    )
    if resolved_days < 1:
        raise ValueError("synthesis.days must be at least 1")
    if resolved_limit < synth.MIN_PAPERS:
        raise ValueError(f"synthesis.max_papers must be at least {synth.MIN_PAPERS}")

    resolved_rank = min_rank if min_rank is not None else section.get("min_rank")
    if resolved_rank is not None:
        resolved_rank = number(resolved_rank, "min_rank", float)

    return {
        "days": resolved_days,
        "max_papers": resolved_limit,
        "min_rank": resolved_rank,
        "model": section.get("model") or None,
        "reasoning_effort": section.get("reasoning_effort") or None,
        "prompt": section.get("prompt") or None,
    }


def _paper(row: Any) -> Dict[str, Any]:
    """A history row as the plain mapping the synthesizer and the stored content use."""
    return {
        "entry_id": row["entry_id"],
        "title": row["title"] or "",
        "link": row["link"] or "",
        "feed_name": row["feed_name"] or "",
        "abstract": row["abstract"] or "",
        "summary": row["summary"] or "",
    }


def run(
    config_path: str,
    topic: Optional[str] = None,
    *,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    min_rank: Optional[float] = None,
    dry_run: bool = False,
    output_json: bool = False,
    client: Any = None,
) -> Dict[str, Any]:
    """Generate and store a synthesis for one topic or for every topic that has it enabled.

    Args:
        config_path: Path to main config
        topic: Topic to synthesize. If None: every topic with ``synthesis.enabled: true``.
        days: Papers matched in the last N days (overrides ``synthesis.days``)
        limit: At most this many papers (overrides ``synthesis.max_papers``)
        min_rank: Only papers with at least this rank score (overrides ``synthesis.min_rank``)
        dry_run: Report which papers would be sent; make no OpenAI call, store nothing
        output_json: When True, suppress log noise
        client: An OpenAI client to use instead of creating one (for tests)

    Returns:
        ``{"command": "synthesize", "topics": {name: {...}}, "stored": n, "failed": n}``.
        Per topic ``status`` is ``stored``, ``dry-run``, ``skipped`` or ``failed``
        (with a ``reason`` for the last two).

    Raises:
        ValueError: invalid configuration.
        RuntimeError: the OpenAI client cannot be created (no ``OPENAI_API_KEY``).
    """
    if output_json:
        logging.getLogger("paper_firehose").setLevel(logging.WARNING)
    logger.info("Starting synthesize command")

    cfg_mgr = ConfigManager(config_path)
    if not cfg_mgr.validate_config():
        raise ValueError("Configuration validation failed")

    config = cfg_mgr.load_config()
    db = DatabaseManager(config)

    results: Dict[str, Dict[str, Any]] = {}
    stored = failed = 0
    synthesizers: Dict[tuple, synth.Synthesizer] = {}

    for topic_name in resolve_topics(cfg_mgr, topic):
        try:
            tcfg = cfg_mgr.load_topic_config(topic_name)
        except Exception as exc:
            logger.error("Failed to load topic '%s': %s", topic_name, exc)
            results[topic_name] = {"status": "failed", "reason": "topic configuration could not be loaded"}
            failed += 1
            continue

        section = tcfg.get("synthesis") or {}
        if topic is None and not (isinstance(section, dict) and section.get("enabled")):
            # Without --topic only topics that opted in are processed (each is a paid call).
            continue

        try:
            settings = _settings(tcfg, days, limit, min_rank)
        except ValueError as exc:
            logger.error("Topic '%s': %s", topic_name, exc)
            results[topic_name] = {"status": "failed", "reason": str(exc)}
            failed += 1
            continue

        until = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
        since = until - datetime.timedelta(days=settings["days"])
        rows, total = db.get_history_matches(
            topic_name, since=since, until=until,
            min_rank=settings["min_rank"], limit=settings["max_papers"],
        )
        papers: List[Dict[str, Any]] = [_paper(row) for row in rows]
        info: Dict[str, Any] = {"papers": len(papers), "total": total, "days": settings["days"]}

        if len(papers) < synth.MIN_PAPERS:
            info.update(status="skipped",
                        reason=f"only {len(papers)} paper(s) matched in the last {settings['days']} day(s)")
            results[topic_name] = info
            logger.info("Topic '%s': %s", topic_name, info["reason"])
            continue

        display_name = tcfg.get("name", topic_name)
        instructions = synth.build_instructions(settings["prompt"], (tcfg.get("ranking") or {}).get("query"))

        if dry_run:
            info.update(
                status="dry-run",
                input_chars=len(synth.build_input(display_name, papers)) + len(instructions),
                titles=[p["title"] for p in papers],
            )
            results[topic_name] = info
            continue

        # One client per (model, effort); created only when a call is really made,
        # so a dry run or a run with nothing to do needs no API key.
        key = (settings["model"], settings["reasoning_effort"])
        if key not in synthesizers:
            try:
                synthesizers[key] = synth.Synthesizer(
                    settings["model"], reasoning_effort=settings["reasoning_effort"], client=client
                )
            except Exception as exc:
                raise RuntimeError(f"Cannot create OpenAI client (is OPENAI_API_KEY set?): {exc}") from exc
        synthesizer = synthesizers[key]

        answer = synthesizer.synthesize(display_name, papers, instructions)
        if answer is None:
            info.update(status="failed", reason="no usable answer from the model (see the log)")
            results[topic_name] = info
            failed += 1
            continue

        content = {
            "overview": answer["overview"],
            "themes": answer["themes"],
            # What the theme numbers point at — stored with the text so that it can
            # be shown later without the history rows (they may have been purged).
            "papers": [
                {"entry_id": p["entry_id"], "title": p["title"], "link": p["link"], "feed_name": p["feed_name"]}
                for p in papers
            ],
        }
        db.save_synthesis(topic_name, since, until, len(papers), synthesizer.model, content)
        info.update(status="stored", themes=len(answer["themes"]), model=synthesizer.model)
        results[topic_name] = info
        stored += 1
        logger.info("Topic '%s': synthesis of %d papers stored (%d themes)",
                    topic_name, len(papers), len(answer["themes"]))

    db.close_all_connections()
    return {"command": "synthesize", "topics": results, "stored": stored, "failed": failed}
