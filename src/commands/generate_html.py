"""
Generate topic HTML directly from the current-run database (papers.db).

This bypasses fetching/filtering and renders HTML for one or all topics
using entries already stored in papers.db (status='filtered').
"""

import logging
from typing import Optional

from core.config import ConfigManager
from core.database import DatabaseManager
from processors.html_generator import HTMLGenerator

logger = logging.getLogger(__name__)


def run(config_path: str, topic: Optional[str] = None) -> None:
    """
    Generate HTML for a specific topic or all topics directly from papers.db.

    Args:
        config_path: Path to the main configuration file
        topic: Optional specific topic to render (if None, render all topics)
    """
    logger.info("Starting HTML generation from database")

    # Initialize components
    config_manager = ConfigManager(config_path)
    if not config_manager.validate_config():
        logger.error("Configuration validation failed")
        return

    config = config_manager.load_config()
    db_manager = DatabaseManager(config)
    html_generator = HTMLGenerator()

    # Determine topics to render
    if topic:
        topics_to_render = [topic]
        logger.info(f"Rendering specific topic: {topic}")
    else:
        topics_to_render = config_manager.get_available_topics()
        logger.info(f"Rendering all topics: {topics_to_render}")

    for topic_name in topics_to_render:
        try:
            topic_config = config_manager.load_topic_config(topic_name)
            output_config = topic_config.get('output', {})
            output_filename = output_config.get('filename', f'{topic_name}_filtered_articles.html')

            # Use the topic's display name as the main heading in HTML
            heading = topic_config.get('name', topic_name)

            # Generate from DB for this topic
            html_generator.generate_html_from_database(
                db_manager,
                topic_name,
                output_filename,
                heading,
            )

            logger.info(f"Generated HTML for topic '{topic_name}': {output_filename}")

            # Always generate ranked HTML from current DB state to avoid stale files
            try:
                ranked_filename = output_config.get('filename_ranked') or f'results_{topic_name}_ranked.html'
                ranked_template = 'ranked_template.html'
                ranked_gen = HTMLGenerator(template_path=ranked_template)
                ranked_gen.generate_ranked_html_from_database(db_manager, topic_name, ranked_filename, heading)
                logger.info(f"Generated ranked HTML for topic '{topic_name}': {ranked_filename}")
            except Exception as e:
                logger.error(f"Failed to generate ranked HTML for topic '{topic_name}': {e}")
        except Exception as e:
            logger.error(f"Error generating HTML for topic '{topic_name}': {e}")
            continue

    db_manager.close_all_connections()
    logger.info("HTML generation from database completed")
