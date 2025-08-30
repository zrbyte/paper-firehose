"""
Configuration management for YAML-based config files.
"""

import yaml
import os
from typing import Dict, Any, List
import logging

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
    
    def get_feeds_for_topic(self, topic_name: str) -> List[str]:
        """Get the list of feeds for a specific topic."""
        topic_config = self.load_topic_config(topic_name)
        return topic_config.get('feeds', [])
    
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
            
            logger.info("Configuration validation passed")
            return True
            
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            return False
