import os
import sys
from typing import Optional, Dict, Any

# Ensure the repository's src/ directory is on sys.path so we can reuse
# the existing implementation without changing its structure.
_PACKAGE_DIR = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(_PACKAGE_DIR)
_SRC_PATH = os.path.join(_REPO_ROOT, 'src')
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from commands import filter as filter_cmd  # type: ignore
from commands import rank as rank_cmd  # type: ignore
from commands import abstracts as abstracts_cmd  # type: ignore
from commands import summarize as summarize_cmd  # type: ignore
from core.config import ConfigManager  # type: ignore
from core.database import DatabaseManager  # type: ignore
from processors.html_generator import HTMLGenerator  # type: ignore

import logging


logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = os.path.join(_REPO_ROOT, 'config', 'config.yaml')

__all__ = [
    'filter',
    'rank',
    'abstracts',
    'summarize',
    'purge',
    'status',
    'generate_html',
]


def filter(topic: Optional[str] = None, config_path: Optional[str] = None) -> None:
    """Run the filter step programmatically.

    Args:
        topic: Optional topic name to process; if None, process all topics.
        config_path: Path to main YAML config; defaults to repo config.
    """
    cfg_path = config_path or _DEFAULT_CONFIG
    filter_cmd.run(cfg_path, topic)


def rank(topic: Optional[str] = None, config_path: Optional[str] = None) -> None:
    """Compute and write rank scores into papers.db for the given topic (or all)."""
    cfg_path = config_path or _DEFAULT_CONFIG
    rank_cmd.run(cfg_path, topic)


def abstracts(
    topic: Optional[str] = None,
    *,
    mailto: Optional[str] = None,
    limit: Optional[int] = None,
    rps: Optional[float] = None,
    config_path: Optional[str] = None,
) -> None:
    """Fetch abstracts for ranked entries and write to papers.db/history.

    Args:
        topic: Restrict to a single topic (optional)
        mailto: Contact email for Crossref UA (optional)
        limit: Max abstracts per topic (optional)
        rps: Requests/second throttle (optional)
        config_path: Path to config (optional)
    """
    cfg_path = config_path or _DEFAULT_CONFIG
    abstracts_cmd.run(cfg_path, topic, mailto=mailto, max_per_topic=limit, rps=rps or 1.0)


def summarize(
    topic: Optional[str] = None,
    *,
    rps: Optional[float] = None,
    config_path: Optional[str] = None,
) -> None:
    """Run LLM summarization and write summaries into papers.db/history.

    Args:
        topic: Single topic (optional)
        rps: Requests/second throttle (optional)
        config_path: Path to config (optional)
    """
    cfg_path = config_path or _DEFAULT_CONFIG
    summarize_cmd.run(cfg_path, topic, rps=rps)


def purge(days: Optional[int] = None, all_data: bool = False, config_path: Optional[str] = None) -> None:
    """Purge entries from databases.

    Args:
        days: When provided, removes entries whose published_date falls within the
              most recent N days (including today) across all databases.
        all_data: If True, clears all databases and reinitializes schemas.
        config_path: Path to main YAML config; defaults to repo config.
    """
    if days is None and not all_data:
        raise ValueError("Specify days or all_data=True")
    cfg_path = config_path or _DEFAULT_CONFIG
    filter_cmd.purge(cfg_path, days, all_data)


def status(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Return configuration and environment status for programmatic use."""
    cfg_path = config_path or _DEFAULT_CONFIG
    info: Dict[str, Any] = {'config_path': cfg_path}
    if not os.path.exists(cfg_path):
        info.update({'valid': False, 'error': f'Config file not found: {cfg_path}'})
        return info
    try:
        cm = ConfigManager(cfg_path)
        valid = cm.validate_config()
        topics = cm.get_available_topics()
        feeds = cm.get_enabled_feeds() if valid else {}
        cfg = cm.load_config()
        db_cfg = cfg.get('database', {}) if isinstance(cfg, dict) else {}
        info.update({
            'valid': bool(valid),
            'topics': topics,
            'enabled_feeds_count': len(feeds) if isinstance(feeds, dict) else 0,
            'db_paths': db_cfg,
        })
        return info
    except Exception as e:
        info.update({'valid': False, 'error': str(e)})
        return info


def generate_html(
    topic: Optional[str] = None,
    output_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> None:
    """Generate HTML for one or all topics directly from papers.db.

    Args:
        topic: Optional topic name. When omitted, HTML is produced for all topics
            defined in the configuration.
        output_path: Optional output path. Only valid when *topic* is provided; when
            generating all topics the configured filenames are used.
        config_path: Path to main YAML config; defaults to repo config.
    """
    cfg_path = config_path or _DEFAULT_CONFIG

    if output_path and not topic:
        raise ValueError("output_path can only be provided when generating a single topic")

    config_manager = ConfigManager(cfg_path)
    if not config_manager.validate_config():
        raise ValueError(f"Invalid configuration at {cfg_path}")

    config = config_manager.load_config()
    db_manager = DatabaseManager(config)

    topics_to_render = [topic] if topic else config_manager.get_available_topics()
    if not topics_to_render:
        db_manager.close_all_connections()
        raise ValueError("No topics available in configuration")

    base_generator = HTMLGenerator()
    ranked_generator = HTMLGenerator(template_path='ranked_template.html')
    summary_generator = HTMLGenerator(template_path='llmsummary_template.html')

    try:
        for topic_name in topics_to_render:
            topic_config = config_manager.load_topic_config(topic_name)
            if not isinstance(topic_config, dict):
                logger.warning("Topic config for '%s' is not a dict; skipping", topic_name)
                continue

            output_config = topic_config.get('output', {}) if isinstance(topic_config, dict) else {}
            topic_output_path = (
                output_path
                if topic and output_path
                else output_config.get('filename', f'{topic_name}_filtered_articles.html')
            )

            heading = topic_config.get('name', topic_name)
            description = topic_config.get('description', f"Articles related to {topic_name}")

            base_generator.generate_html_from_database(
                db_manager,
                topic_name,
                topic_output_path,
                heading,
                description,
            )

            ranked_output_path = output_config.get('filename_ranked') or f'results_{topic_name}_ranked.html'
            try:
                ranked_generator.generate_ranked_html_from_database(
                    db_manager,
                    topic_name,
                    ranked_output_path,
                    heading,
                    description,
                )
            except Exception as exc:
                logger.error("Failed to generate ranked HTML for topic '%s': %s", topic_name, exc)

            summary_output_path = output_config.get('filename_summary')
            if summary_output_path:
                summary_title = f"LLM Summaries - {heading}"
                try:
                    summary_generator.generate_summarized_html_from_database(
                        db_manager,
                        topic_name,
                        summary_output_path,
                        summary_title,
                    )
                except Exception as exc:
                    logger.error("Failed to generate summarized HTML for topic '%s': %s", topic_name, exc)
    finally:
        db_manager.close_all_connections()
