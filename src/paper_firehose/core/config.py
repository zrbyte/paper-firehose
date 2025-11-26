"""Configuration management for YAML-based config files."""

import os
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .paths import get_data_dir, get_system_path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = get_data_dir() / "config"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"
_TEMPLATE_DIR = get_system_path("config")
_TEMPLATE_CONFIG = _TEMPLATE_DIR / "config.yaml"
_TEMPLATE_TOPICS_DIR = _TEMPLATE_DIR / "topics"
_TEMPLATE_SECRETS_DIR = _TEMPLATE_DIR / "secrets"

_DEFAULT_EMAIL_SECRET = "# Placeholder SMTP password file. Replace with real credentials.\n"

_DEFAULT_CONFIG_TEMPLATE = """# Auto-generated default configuration for paper-firehose
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "matched_entries_history.db"

llm:
  model: "gpt-5-mini"
  api_key_env: "OPENAI_API_KEY"
  model_fallback: "gpt-5-nano"
  rps: 0.5
  max_retries: 3

paperqa:
  download_rank_threshold: 0.35
  rps: 0.3
  max_retries: 3
  prompt: >
    Provide a concise summary of the paper in JSON with keys "summary" and "methods".

feeds:
  cond-mat:
    name: "arXiv cond-mat"
    url: "https://rss.arxiv.org/rss/cond-mat"
    enabled: true

priority_journals: []

defaults:
  time_window_days: 365
  top_n_per_topic: 10
  rank_threshold: 0.3
  ranking_negative_penalty: 0.25
"""

_DEFAULT_TOPIC_TEMPLATE = """name: "example"
description: "Auto-generated starter topic. Update the regex and feeds for your workflow."

feeds:
  - "cond-mat"

filter:
  pattern: "graphene"
  fields: ["title", "summary"]

ranking:
  query: >
    graphene
    condensed matter
"""


def _write_template(path: Path, content: str) -> None:
    """Write templated YAML content to disk with a trailing newline."""
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _copy_tree(src: Path, dest: Path) -> bool:
    """Copy files from *src* to *dest* without overwriting existing files."""

    if not src.exists():
        return False

    created = False
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            if _copy_tree(item, target):
                created = True
        else:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, target)
                created = True
    return created


class ConfigManager:
    """Manages loading and validation of YAML configuration files."""

    def __init__(self, config_path: Optional[str] = None):
        """Initialize the manager and ensure baseline config/topic files exist."""
        path = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        self.config_path = str(path)
        self.base_dir = str(path.parent)
        self._config = None
        self._topics = {}
        self._ensure_default_config()
    
    def load_config(self) -> Dict[str, Any]:
        """Load the main configuration file."""
        if self._config is None:
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f)
                logger.info(f"Loaded configuration from {self.config_path}")
            except Exception as e:
                logger.error(f"Failed to load config from {self.config_path}: {e}")
                raise
        
        return self._config
    
    def _resolve_topic_path(self, topic_name: str) -> Path:
        """Return the filesystem path for *topic_name* supporting .yaml and .yml."""
        topics_dir = Path(self.base_dir) / "topics"
        candidates = [topics_dir / f"{topic_name}.yaml", topics_dir / f"{topic_name}.yml"]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        # Final fallback: scan the directory in case the caller used mixed case
        # or the file includes extra dots in its name (e.g., topic.test.yaml).
        pattern = f"{topic_name}.*"
        for candidate in topics_dir.glob(pattern):
            if candidate.suffix.lower() in {".yaml", ".yml"}:
                return candidate

        raise FileNotFoundError(
            f"Topic configuration file for '{topic_name}' not found (.yaml or .yml) in {topics_dir}"
        )

    def load_topic_config(self, topic_name: str) -> Dict[str, Any]:
        """Load a topic-specific configuration file."""
        if topic_name not in self._topics:
            topic_path = self._resolve_topic_path(topic_name)
            try:
                with open(topic_path, 'r', encoding='utf-8') as f:
                    self._topics[topic_name] = yaml.safe_load(f)
                logger.info("Loaded topic config for '%s' from %s", topic_name, topic_path)
            except Exception as e:
                logger.error("Failed to load topic config from %s: %s", topic_path, e)
                raise

        return self._topics[topic_name]

    def _ensure_default_config(self) -> None:
        """Create default configuration files if they are missing."""

        config_file = Path(self.config_path)
        config_file.parent.mkdir(parents=True, exist_ok=True)

        if not config_file.exists():
            if _TEMPLATE_CONFIG.exists():
                try:
                    shutil.copyfile(_TEMPLATE_CONFIG, config_file)
                    logger.info("Created default config.yaml at %s", config_file)
                except Exception as exc:
                    logger.warning("Failed to copy template config: %s", exc)
                    _write_template(config_file, _DEFAULT_CONFIG_TEMPLATE)
            else:
                _write_template(config_file, _DEFAULT_CONFIG_TEMPLATE)
                logger.info("Created fallback default config.yaml at %s", config_file)

        topics_dir = Path(self.base_dir) / "topics"
        secrets_dir = Path(self.base_dir) / "secrets"

        # Only seed templates if directories don't exist (one-time initialization)
        topics_existed = topics_dir.exists()
        secrets_existed = secrets_dir.exists()

        topics_dir.mkdir(parents=True, exist_ok=True)
        secrets_dir.mkdir(parents=True, exist_ok=True)

        created_topic = False
        if not topics_existed:
            try:
                if _copy_tree(_TEMPLATE_TOPICS_DIR, topics_dir):
                    created_topic = True
            except Exception as exc:
                logger.warning("Failed to copy topics template tree: %s", exc)

        if not secrets_existed:
            try:
                _copy_tree(_TEMPLATE_SECRETS_DIR, secrets_dir)
            except Exception as exc:
                logger.warning("Failed to copy secrets template tree: %s", exc)

            # Ensure critical secret placeholders exist even if the template tree lacks them
            placeholders = {
                "email_password.env": _DEFAULT_EMAIL_SECRET,
            }
            for filename, content in placeholders.items():
                target = secrets_dir / filename
                if target.exists():
                    continue
                try:
                    target.write_text(content, encoding="utf-8")
                except Exception as exc:
                    logger.warning("Failed to create placeholder secret %s: %s", target, exc)

        if not any(topics_dir.glob("*.yml")) and not any(topics_dir.glob("*.yaml")):
            default_topic_path = topics_dir / "example.yaml"
            _write_template(default_topic_path, _DEFAULT_TOPIC_TEMPLATE)
            created_topic = True
            logger.info("Created fallback default topic config at %s", default_topic_path)

        if _TEMPLATE_DIR.exists():
            for item in _TEMPLATE_DIR.iterdir():
                if not item.is_dir() or item.name in {"topics", "secrets"}:
                    continue
                dest_dir = Path(self.base_dir) / item.name
                # Only seed templates if directory doesn't exist (one-time initialization)
                dest_existed = dest_dir.exists()
                dest_dir.mkdir(parents=True, exist_ok=True)
                if not dest_existed:
                    try:
                        _copy_tree(item, dest_dir)
                    except Exception as exc:
                        logger.warning("Failed to copy template directory %s: %s", item, exc)

        if created_topic:
            self._topics.clear()
    
    def get_available_topics(self) -> List[str]:
        """Get list of available topic configuration files."""
        topics_dir = os.path.join(self.base_dir, "topics")
        if not os.path.exists(topics_dir):
            return []
        
        topics = []
        for filename in os.listdir(topics_dir):
            if filename.endswith('.yaml') or filename.endswith('.yml'):
                topic_name = os.path.splitext(filename)[0]
                topics.append(topic_name)
        
        return topics
    
    # Note: `get_feeds_for_topic` removed as unused by current code paths.
    
    def get_enabled_feeds(self) -> Dict[str, Dict[str, Any]]:
        """Get all enabled feeds from the main configuration."""
        config = self.load_config()
        feeds = config.get('feeds', {})
        
        enabled_feeds = {}
        for feed_name, feed_config in feeds.items():
            if feed_config.get('enabled', True):
                enabled_feeds[feed_name] = feed_config
        
        return enabled_feeds
    
    def get_priority_journals(self) -> List[str]:
        """Get the list of priority journals."""
        config = self.load_config()
        return config.get('priority_journals', [])
    
    def validate_config(self) -> bool:
        """Validate the configuration files."""
        try:
            # Validate main config
            config = self.load_config()
            
            required_sections = ['database', 'feeds']
            for section in required_sections:
                if section not in config:
                    logger.error(f"Missing required section '{section}' in main config")
                    return False
            
            # Validate database paths
            db_config = config['database']
            required_db_keys = ['path', 'all_feeds_path', 'history_path']
            for key in required_db_keys:
                if key not in db_config:
                    logger.error(f"Missing required database path '{key}'")
                    return False

            # Validate priority_journals keys and optional boost type
            priority_journals = config.get('priority_journals', [])
            if priority_journals is not None and not isinstance(priority_journals, list):
                logger.error("'priority_journals' must be a list of feed keys in config.yaml")
                return False
            if isinstance(priority_journals, list):
                available_feeds = list(config['feeds'].keys())
                for feed_key in priority_journals:
                    if feed_key not in available_feeds:
                        logger.warning(f"priority_journals contains unknown feed key '{feed_key}'")
            # Optional global boost
            if 'priority_journal_boost' in config:
                pj_boost = config.get('priority_journal_boost')
                if not isinstance(pj_boost, (int, float)):
                    logger.error("'priority_journal_boost' must be a number (int/float)")
                    return False
            
            # Validate topic configs
            topics = self.get_available_topics()
            for topic in topics:
                topic_config = self.load_topic_config(topic)
                
                # Check required fields
                required_topic_keys = ['name', 'feeds', 'filter']
                for key in required_topic_keys:
                    if key not in topic_config:
                        logger.error(f"Missing required key '{key}' in topic '{topic}'")
                        return False
                
                # Validate feeds exist in main config
                topic_feeds = topic_config['feeds']
                available_feeds = list(config['feeds'].keys())
                for feed in topic_feeds:
                    if feed not in available_feeds:
                        logger.error(f"Topic '{topic}' references unknown feed '{feed}'")
                        return False

                # Validate filter pattern presence and compilability
                filter_cfg = topic_config.get('filter', {})
                pattern = filter_cfg.get('pattern')
                if not isinstance(pattern, str) or not pattern.strip():
                    logger.error(f"Topic '{topic}' filter.pattern must be a non-empty string")
                    return False
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as e:
                    logger.error(f"Topic '{topic}' filter.pattern is not a valid regex: {e}")
                    return False

                # Optional ranking config validation
                ranking_cfg = topic_config.get('ranking', {}) or {}
                if ranking_cfg:
                    neg = ranking_cfg.get('negative_queries')
                    if neg is not None:
                        if not isinstance(neg, list) or not all(isinstance(x, str) for x in neg):
                            logger.error(f"Topic '{topic}' ranking.negative_queries must be a list of strings")
                            return False
                    pref = ranking_cfg.get('preferred_authors')
                    if pref is not None:
                        if not isinstance(pref, list) or not all(isinstance(x, str) for x in pref):
                            logger.error(f"Topic '{topic}' ranking.preferred_authors must be a list of strings")
                            return False
                    pab = ranking_cfg.get('priority_author_boost')
                    if pab is not None and not isinstance(pab, (int, float)):
                        logger.error(f"Topic '{topic}' ranking.priority_author_boost must be a number (int/float)")
                        return False
            
            logger.info("Configuration validation passed")
            return True
            
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            return False


__all__ = [
    "ConfigManager",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_CONFIG_DIR",
]
