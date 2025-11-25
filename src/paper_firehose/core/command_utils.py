"""Shared utilities for command implementations.

Provides common patterns used across multiple commands.
"""

from typing import Optional, List
from .config import ConfigManager


def resolve_topics(config_manager: ConfigManager, topic: Optional[str] = None) -> List[str]:
    """Resolve topic argument to list of topics to process.

    If a specific topic is provided, returns a list containing just that topic.
    Otherwise, returns all available topics from the configuration.

    Args:
        config_manager: Configuration manager instance
        topic: Optional specific topic name. If None, all topics are returned.

    Returns:
        List of topic names to process

    Examples:
        >>> cfg = ConfigManager()
        >>> resolve_topics(cfg, "physics")  # Returns ["physics"]
        >>> resolve_topics(cfg, None)  # Returns all topics like ["physics", "biology", ...]
    """
    if topic:
        return [topic]
    return config_manager.get_available_topics()
