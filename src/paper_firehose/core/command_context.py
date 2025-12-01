"""
Command context for shared initialization across CLI commands.

Provides a unified way to initialize config, database, and common parameters
to reduce boilerplate code in command implementations.
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from .config import ConfigManager
from .database import DatabaseManager


logger = logging.getLogger(__name__)


class CommandContext:
    """Encapsulates shared initialization logic for CLI commands.

    Handles config loading, validation, database connection, and topic resolution
    in a single reusable class to reduce boilerplate across commands.

    Example:
        ```python
        ctx = CommandContext(config_path)
        for topic in ctx.get_topics(topic_arg):
            # Access ctx.config_manager, ctx.config, ctx.db
            entries = ctx.db.get_current_entries(topic=topic)
        ```
    """

    def __init__(self, config_path: Optional[str] = None):
        """Initialize command context with config and database.

        Args:
            config_path: Path to main config file (None = use default)

        Raises:
            ValueError: If configuration is invalid
        """
        self.config_manager = ConfigManager(config_path)

        # Validate configuration
        if not self.config_manager.validate_config():
            raise ValueError("Invalid configuration. Run 'paper-firehose status' for details.")

        self.config = self.config_manager.load_config()
        self.db = DatabaseManager(self.config)

        logger.debug(f"CommandContext initialized with config from {self.config_manager.config_path}")

    def get_topics(self, topic: Optional[str] = None) -> list[str]:
        """Resolve topic argument to list of topics to process.

        Args:
            topic: Optional single topic name, or None for all topics

        Returns:
            List of topic names (single topic or all available topics)
        """
        if topic:
            return [topic]
        return self.config_manager.get_available_topics()

    def load_topic_config(self, topic: str) -> Dict[str, Any]:
        """Load configuration for a specific topic.

        Args:
            topic: Topic name

        Returns:
            Topic configuration dictionary

        Raises:
            FileNotFoundError: If topic config file doesn't exist
        """
        return self.config_manager.load_topic_config(topic)

    def get_default(self, key: str, default: Any = None) -> Any:
        """Get value from defaults section of config.

        Args:
            key: Config key (e.g., 'rank_threshold')
            default: Fallback value if key not found

        Returns:
            Config value or default
        """
        defaults = self.config.get('defaults') or {}
        return defaults.get(key, default)

    def get_nested_default(self, *keys: str, default: Any = None) -> Any:
        """Get nested value from defaults section.

        Args:
            *keys: Nested keys (e.g., 'abstracts', 'mailto')
            default: Fallback value if path not found

        Returns:
            Config value or default

        Example:
            ```python
            # Get config.defaults.abstracts.mailto
            mailto = ctx.get_nested_default('abstracts', 'mailto', default='noreply@example.com')
            ```
        """
        defaults = self.config.get('defaults') or {}
        value = defaults
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
        return value if value is not None else default

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup resources."""
        # DatabaseManager handles its own cleanup via context managers
        # No explicit cleanup needed here
        pass
