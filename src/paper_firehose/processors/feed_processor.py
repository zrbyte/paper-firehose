"""
RSS feed processing functionality.
Fetches RSS feeds, applies regex filters, and manages entry storage.
"""

import feedparser
import re
import time
import datetime
from typing import Dict, List, Any
import logging

from ..core.database import DatabaseManager
from ..core.config import ConfigManager

logger = logging.getLogger(__name__)

# Default time window for processing entries (days); can be overridden by config.defaults.time_window_days
DEFAULT_TIME_WINDOW_DAYS = 365


class FeedProcessor:
    """Processes RSS feeds with regex filtering and database storage."""
    
    def __init__(self, db_manager: DatabaseManager, config_manager: ConfigManager):
        """Bind database/config managers and derive the time window constraint."""
        self.db = db_manager
        self.config = config_manager
        # Resolve time window from config (defaults.time_window_days)
        cfg = self.config.load_config()
        days = int((cfg.get('defaults') or {}).get('time_window_days', DEFAULT_TIME_WINDOW_DAYS))
        self.time_delta = datetime.timedelta(days=days)
    
    def fetch_feeds(self, topic_name: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch RSS feeds for a topic and return new entries.
        
        Returns:
            Dict mapping feed names to lists of new entries
        """
        topic_config = self.config.load_topic_config(topic_name)
        feeds_to_process = topic_config['feeds']
        enabled_feeds = self.config.get_enabled_feeds()
        
        new_entries_per_feed = {}
        current_time = datetime.datetime.now()
        
        for feed_key in feeds_to_process:
            if feed_key not in enabled_feeds:
                logger.warning(f"Feed '{feed_key}' not enabled, skipping")
                continue
            
            feed_config = enabled_feeds[feed_key]
            feed_url = feed_config['url']
            feed_display_name = feed_config.get('name', feed_key)
            
            logger.info(f"Processing feed '{feed_display_name}' for topic '{topic_name}'")
            
            try:
                # Fetch and parse RSS feed
                feed = feedparser.parse(feed_url)
                if feed.bozo:
                    logger.warning(f"Feed '{feed_display_name}' has parsing issues: {feed.bozo_exception}")
                
                feed_entries = feed.entries
                logger.debug(f"Feed '{feed_display_name}' returned {len(feed_entries)} raw entries")
                feed_title = getattr(feed.feed, 'title', feed_display_name)
                
                # Add feed metadata to each entry
                for entry in feed_entries:
                    entry['feed_title'] = feed_title
                
                new_entries = []
                
                for entry in feed_entries:
                    # Generate stable entry ID
                    entry_id = self.db.compute_entry_id(entry)
                    
                    # Check if entry is within time window
                    entry_published = entry.get('published_parsed') or entry.get('updated_parsed')
                    if entry_published:
                        if isinstance(entry_published, time.struct_time):
                            entry_datetime = datetime.datetime(*entry_published[:6])
                        else:
                            entry_datetime = entry_published
                    else:
                        entry_datetime = current_time
                    
                    # Skip entries older than configured time window
                    if (current_time - entry_datetime) > self.time_delta:
                        continue
                    
                    # Check if this is a new entry (by title)
                    title = entry.get('title', '').strip()
                    if self.db.is_new_entry(title):
                        new_entries.append(entry)
                        logger.debug(f"New entry found: {title[:50]}...")
                
                new_entries_per_feed[feed_key] = new_entries
                logger.info(f"Found {len(new_entries)} new entries in feed '{feed_display_name}'")
                
            except Exception as e:
                logger.error(f"Error processing feed '{feed_display_name}': {e}")
                new_entries_per_feed[feed_key] = []
        
        return new_entries_per_feed
    
    def apply_filters(self, entries_per_feed: Dict[str, List[Dict[str, Any]]], topic_name: str) -> List[Dict[str, Any]]:
        """
        Apply regex filters to entries and return matched entries.
        
        Args:
            entries_per_feed: Dict mapping feed names to entry lists
            topic_name: Name of the topic to filter for
            
        Returns:
            List of entries that match the topic's regex filter
        """
        topic_config = self.config.load_topic_config(topic_name)
        filter_config = topic_config['filter']
        
        pattern = filter_config['pattern']
        fields = filter_config.get('fields', ['title', 'summary'])
        
        # Compile regex pattern
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            logger.error(f"Invalid regex pattern for topic '{topic_name}': {e}")
            return []
        
        matched_entries = []
        priority_journals = self.config.get_priority_journals()
        enabled_feeds = self.config.get_enabled_feeds()

        for feed_key, entries in entries_per_feed.items():
            is_priority_feed = feed_key in priority_journals
            feed_display_name = enabled_feeds.get(feed_key, {}).get('name', feed_key)
            
            for entry in entries:
                entry_id = self.db.compute_entry_id(entry)
                
                # Check if entry matches regex pattern
                matches_regex = self._matches_pattern(entry, regex, fields)
                
                # Only include entries that match the regex pattern
                # Priority status is preserved for future LLM ranking/summarization
                if matches_regex:
                    # Add metadata
                    entry['entry_id'] = entry_id
                    entry['feed_name'] = feed_display_name
                    entry['topic'] = topic_name
                    entry['is_priority'] = is_priority_feed
                    
                    # Save to matched_entries_history.db if topic has archive: true
                    topic_config = self.config.load_topic_config(topic_name)
                    output_config = topic_config.get('output', {})
                    if output_config.get('archive', False):
                        self.db.save_matched_entry(entry, feed_display_name, topic_name, entry_id)
                    
                    # Save to papers.db for current run processing
                    self.db.save_current_entry(entry, feed_display_name, topic_name, entry_id)
                    
                    matched_entries.append(entry)
                    
                    logger.debug(f"Entry matched for topic '{topic_name}': {entry.get('title', 'No title')[:50]}... (priority: {is_priority_feed})")
        
        logger.info(f"Found {len(matched_entries)} entries matching filters for topic '{topic_name}'")
        return matched_entries
    
    def _matches_pattern(self, entry: Dict[str, Any], regex: re.Pattern, fields: List[str]) -> bool:
        """Check if entry matches the regex pattern in specified fields."""
        for field in fields:
            text = ""
            if field == 'title':
                text = entry.get('title', '')
            elif field == 'summary':
                text = entry.get('summary', entry.get('description', ''))
            elif field == 'authors':
                authors = entry.get('authors', [])
                if authors:
                    text = ', '.join(author.get('name', '') for author in authors)
                else:
                    text = entry.get('author', '')
            
            if text and regex.search(text):
                return True
        
        return False
    
    def save_all_entries_to_dedup_db(self, all_entries_per_feed: Dict[str, List[Dict[str, Any]]]):
        """Save ALL processed entries to all_feed_entries.db for deduplication."""
        enabled_feeds = self.config.get_enabled_feeds()
        for feed_key, entries in all_entries_per_feed.items():
            display_name = enabled_feeds.get(feed_key, {}).get('name', feed_key)
            for entry in entries:
                entry_id = self.db.compute_entry_id(entry)
                self.db.save_feed_entry(entry, display_name, entry_id)
        
        logger.info(f"Saved all processed entries to deduplication database")
    
