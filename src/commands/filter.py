"""
Filter command implementation.
Fetches RSS feeds, applies regex filters, and generates HTML output.
"""

import os
import logging
from typing import Optional

from core.config import ConfigManager
from core.database import DatabaseManager
from processors.feed_processor import FeedProcessor
from processors.html_generator import HTMLGenerator

logger = logging.getLogger(__name__)


def run(config_path: str, topic: Optional[str] = None) -> None:
    """
    Run the filter command.
    
    1. Load config and topic definitions
    2. Fetch RSS feeds for each topic
    3. Apply regex filters
    4. Generate HTML output
    5. Store filtered entries in database with status='filtered'
    
    Args:
        config_path: Path to the main configuration file
        topic: Optional specific topic to process (if None, process all topics)
    """
    logger.info("Starting filter command")
    
    try:
        # Initialize components
        config_manager = ConfigManager(config_path)
        
        # Validate configuration
        if not config_manager.validate_config():
            logger.error("Configuration validation failed")
            return
        
        # Load main config
        config = config_manager.load_config()
        
        # Initialize database manager
        db_manager = DatabaseManager(config)
        
        # Clear current run database
        db_manager.clear_current_db()
        
        # Initialize processors
        feed_processor = FeedProcessor(db_manager, config_manager)
        html_generator = HTMLGenerator()
        
        # Determine topics to process
        if topic:
            topics_to_process = [topic]
            logger.info(f"Processing specific topic: {topic}")
        else:
            topics_to_process = config_manager.get_available_topics()
            logger.info(f"Processing all topics: {topics_to_process}")
        
        # Process each topic
        for topic_name in topics_to_process:
            try:
                logger.info(f"Processing topic: {topic_name}")
                
                # Load topic configuration
                topic_config = config_manager.load_topic_config(topic_name)
                
                # Process topic: fetch feeds and apply filters
                matched_entries = feed_processor.process_topic(topic_name)
                
                # Generate HTML output
                output_config = topic_config.get('output', {})
                output_filename = output_config.get('filename', f'{topic_name}_filtered_articles.html')
                output_path = output_filename
                
                # Generate HTML
                html_generator.generate_topic_html(
                    topic_name=topic_name,
                    entries=matched_entries,
                    output_path=output_path,
                    topic_config=topic_config
                )
                
                logger.info(f"Completed processing topic '{topic_name}': {len(matched_entries)} entries, output: {output_path}")
                
            except Exception as e:
                logger.error(f"Error processing topic '{topic_name}': {e}")
                continue
        
        # Close database connections
        db_manager.close_all_connections()
        
        logger.info("Filter command completed successfully")
        
    except Exception as e:
        logger.error(f"Filter command failed: {e}")
        raise


def purge(config_path: str, days: Optional[int] = None, all_data: bool = False) -> None:
    """
    Purge old entries from databases.
    
    Args:
        config_path: Path to the main configuration file
        days: Number of days to keep (if None and not all_data, keep all)
        all_data: If True, clear all databases completely
    """
    logger.info("Starting purge command")
    
    try:
        # Initialize components
        config_manager = ConfigManager(config_path)
        config = config_manager.load_config()
        db_manager = DatabaseManager(config)
        
        if all_data:
            logger.info("Purging all data from databases")
            # Clear all databases
            for db_path in db_manager.db_paths.values():
                if os.path.exists(db_path):
                    os.remove(db_path)
                    logger.info(f"Removed database: {db_path}")
            
            # Reinitialize databases
            db_manager._init_databases()
            logger.info("Databases reinitialized")
            
        elif days is not None:
            logger.info(f"Purging entries older than {days} days")
            db_manager.purge_old_entries(days)
            logger.info(f"Purged entries older than {days} days")
        
        else:
            logger.warning("No purge action specified (use --days X or --all)")
        
        db_manager.close_all_connections()
        logger.info("Purge command completed")
        
    except Exception as e:
        logger.error(f"Purge command failed: {e}")
        raise
