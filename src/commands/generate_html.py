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

            # Use the topic's display name and description
            heading = topic_config.get('name', topic_name)
            subheading = topic_config.get('description')

            # Generate from DB for this topic
            html_generator.generate_html_from_database(
                db_manager,
                topic_name,
                output_filename,
                heading,
                subheading,
            )

            logger.info(f"Generated HTML for topic '{topic_name}': {output_filename}")

            # Always generate ranked HTML from current DB state to avoid stale files
            try:
                ranked_filename = output_config.get('filename_ranked') or f'results_{topic_name}_ranked.html'
                ranked_template = 'ranked_template.html'
                ranked_gen = HTMLGenerator(template_path=ranked_template)
                ranked_gen.generate_ranked_html_from_database(db_manager, topic_name, ranked_filename, heading, subheading)
                logger.info(f"Generated ranked HTML for topic '{topic_name}': {ranked_filename}")
            except Exception as e:
                logger.error(f"Failed to generate ranked HTML for topic '{topic_name}': {e}")
        except Exception as e:
            logger.error(f"Error generating HTML for topic '{topic_name}': {e}")
            continue

    # Generate summarized HTML for each topic that has summaries
    try:
        html_gen = HTMLGenerator(template_path="llmsummary_template.html")
        
        for topic_name in topics_to_render:
            try:
                topic_config = config_manager.load_topic_config(topic_name)
                output_config = topic_config.get('output', {})
                summary_filename = output_config.get('filename_summary')
                
                if summary_filename:
                    # Check if this topic has any summaries (PQA preferred, else LLM)
                    topic_entries = db_manager.get_current_entries(topic=topic_name)
                    has_summaries = any(
                        ((e.get('paper_qa_summary') or '').strip()) or ((e.get('llm_summary') or '').strip())
                        for e in topic_entries
                    )

                    if has_summaries:
                        topic_display_name = topic_config.get('name', topic_name)
                        html_gen.generate_summarized_html_from_database(
                            db_manager,
                            topic_name,
                            summary_filename,
                            f"LLM Summaries - {topic_display_name}"
                        )
                        logger.info("Generated summarized HTML for topic '%s': %s", topic_name, summary_filename)
            except Exception as e:
                logger.error("Failed to generate summarized HTML for topic '%s': %s", topic_name, e)
    except Exception as e:
        logger.error("Failed to generate summarized HTML: %s", e)

    db_manager.close_all_connections()
    logger.info("HTML generation from database completed")
