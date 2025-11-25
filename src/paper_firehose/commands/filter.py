"""
Filter command implementation.
Fetches RSS feeds, applies regex filters, and writes results to databases.
HTML rendering is handled exclusively by the `html` command.
"""

import os
import logging
from typing import Optional

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.command_utils import resolve_topics
from ..processors.feed_processor import FeedProcessor

logger = logging.getLogger(__name__)


def run(config_path: str, topic: Optional[str] = None) -> None:
    """Run the filtering pipeline for one or all topics.

    Workflow:
    1. Load and validate configuration, then prepare database state (backup + clear current run DB).
    2. Fetch the configured feeds for each topic, yielding only new entries within the time window.
    3. Apply the topic regex filters, writing matches to ``papers.db`` and to the history DB when enabled.
    4. Record every processed entry in the deduplication database for future runs.

    Args:
        config_path: Path to the main configuration file
        topic: Optional specific topic to process (if ``None``, process every topic)
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
        
        # Local safety: backup important databases before we modify them
        db_manager.backup_important_databases()
        
        # Clear current run database
        db_manager.clear_current_db()
        
        # Initialize processors
        feed_processor = FeedProcessor(db_manager, config_manager)
        
        # Determine topics to process
        topics_to_process = resolve_topics(config_manager, topic)
        if topic:
            logger.info(f"Processing specific topic: {topic}")
        else:
            logger.info(f"Processing all topics: {topics_to_process}")
        
        # Process each topic
        all_processed_entries = {}  # Track all entries for saving to dedup DB later
        
        for topic_name in topics_to_process:
            try:
                logger.info(f"Processing topic: {topic_name}")
                
                # Load topic configuration
                topic_config = config_manager.load_topic_config(topic_name)
                
                # Fetch feeds first (don't save to dedup DB yet)
                entries_per_feed = feed_processor.fetch_feeds(topic_name)
                # Debug: summarize fetched counts per feed
                try:
                    fetched_total = sum(len(v) for v in entries_per_feed.values())
                    logger.info(f"Fetched {fetched_total} new entries across {len(entries_per_feed)} feeds for topic '{topic_name}'")
                    for fk, lst in entries_per_feed.items():
                        logger.debug(f"  Feed '{fk}' fetched {len(lst)} new entries (post-dedup)")
                except Exception:
                    pass
                
                # Collect all entries for later saving to dedup DB
                for feed_name, entries in entries_per_feed.items():
                    if feed_name not in all_processed_entries:
                        all_processed_entries[feed_name] = []
                    all_processed_entries[feed_name].extend(entries)
                
                # Apply filters and save to papers.db/history.db as appropriate
                matched_entries = feed_processor.apply_filters(entries_per_feed, topic_name)
                
                logger.info(f"Completed processing topic '{topic_name}': {len(matched_entries)} entries")
                
            except Exception as e:
                logger.error(f"Error processing topic '{topic_name}': {e}")
                continue
        
        # Save ALL processed entries to deduplication database
        if all_processed_entries:
            feed_processor.save_all_entries_to_dedup_db(all_processed_entries)
        
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
        
        # Safety: backup important databases before purge
        db_manager.backup_important_databases()
        
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
            logger.info(f"Purging entries from the most recent {days} days (including today)")
            db_manager.purge_old_entries(days)
            logger.info(f"Purge completed for entries from the most recent {days} days")
        
        else:
            logger.warning("No purge action specified (use --days X or --all)")
        
        db_manager.close_all_connections()
        logger.info("Purge command completed")
        
    except Exception as e:
        logger.error(f"Purge command failed: {e}")
        raise
