"""
Configuration management for YAML-based config files.
"""

import yaml
import os
from typing import Dict, Any, List
import logging
import re

logger = logging.getLogger(__name__)


class ConfigManager:
    """Manages loading and validation of YAML configuration files."""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = config_path
        self.base_dir = os.path.dirname(config_path)
        self._config = None
        self._topics = {}
    
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
    
    def load_topic_config(self, topic_name: str) -> Dict[str, Any]:
        """Load a topic-specific configuration file."""
        if topic_name not in self._topics:
            topic_path = os.path.join(self.base_dir, "topics", f"{topic_name}.yaml")
            try:
                with open(topic_path, 'r', encoding='utf-8') as f:
                    self._topics[topic_name] = yaml.safe_load(f)
                logger.info(f"Loaded topic config for '{topic_name}' from {topic_path}")
            except Exception as e:
                logger.error(f"Failed to load topic config from {topic_path}: {e}")
                raise
        
        return self._topics[topic_name]
    
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
