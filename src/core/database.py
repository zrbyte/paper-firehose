"""
Database management for the three-database approach:
- all_feed_entries.db: All RSS entries for deduplication
- matched_entries_history.db: Historical matches across all topics
- papers.db: Current run processing data
"""

import sqlite3
import json
import os
import datetime
import hashlib
import urllib.parse
from typing import Dict, List, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages the three-database system for feed processing."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.db_paths = {
            'all_feeds': config['database']['all_feeds_path'],
            'history': config['database']['history_path'],
            'current': config['database']['path']
        }
        
        # Ensure assets directory exists
        for path in self.db_paths.values():
            os.makedirs(os.path.dirname(path), exist_ok=True)
        
        self._init_databases()
    
    def _init_databases(self):
        """Initialize all three databases with proper schemas."""
        # Initialize all_feed_entries.db
        self._init_all_feeds_db()
        
        # Initialize matched_entries_history.db
        self._init_history_db()
        
        # Initialize papers.db (current run)
        self._init_current_db()
    
    def _init_all_feeds_db(self):
        """Initialize the all RSS entries database."""
        conn = sqlite3.connect(self.db_paths['all_feeds'])
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feed_entries (
                entry_id TEXT PRIMARY KEY,
                feed_name TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                authors TEXT,
                published_date TEXT,
                first_seen TEXT DEFAULT (datetime('now')),
                last_seen TEXT DEFAULT (datetime('now')),
                raw_data TEXT,
                UNIQUE(feed_name, entry_id)
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_feed_entries_feed_name 
            ON feed_entries(feed_name)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_feed_entries_first_seen 
            ON feed_entries(first_seen)
        ''')
        
        conn.commit()
        conn.close()
    
    def _init_history_db(self):
        """Initialize the historical matches database."""
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()
        
        # Check if we need to migrate from old schema
        cursor.execute("PRAGMA table_info(matched_entries)")
        table_info = cursor.fetchall()
        columns = [row[1] for row in table_info]
        
        if 'topic' in columns and 'topics' not in columns:
            # Migrate from old schema to new schema
            logger.info("Migrating matched_entries table from 'topic' to 'topics' field")
            
            # Create new table with new schema
            cursor.execute('''
                CREATE TABLE matched_entries_new (
                    entry_id TEXT PRIMARY KEY,
                    feed_name TEXT NOT NULL,
                    topics TEXT NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    summary TEXT,
                    authors TEXT,
                    published_date TEXT,
                    matched_date TEXT DEFAULT (datetime('now')),
                    raw_data TEXT
                )
            ''')
            
            # Copy data, converting topic to topics
            cursor.execute('''
                INSERT INTO matched_entries_new 
                SELECT entry_id, feed_name, topic, title, link, summary, authors, 
                       published_date, matched_date, raw_data
                FROM matched_entries
            ''')
            
            # Drop old table and rename new one
            cursor.execute('DROP TABLE matched_entries')
            cursor.execute('ALTER TABLE matched_entries_new RENAME TO matched_entries')
            
            logger.info("Migration completed successfully")
        
        # Create table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matched_entries (
                entry_id TEXT PRIMARY KEY,
                feed_name TEXT NOT NULL,
                topics TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                authors TEXT,
                published_date TEXT,
                matched_date TEXT DEFAULT (datetime('now')),
                raw_data TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_matched_entries_topics 
            ON matched_entries(topics)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_matched_entries_matched_date 
            ON matched_entries(matched_date)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_matched_entries_entry_id 
            ON matched_entries(entry_id)
        ''')
        
        conn.commit()
        conn.close()
    
    def _init_current_db(self):
        """Initialize the current run processing database."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                feed_name TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                authors TEXT,
                published_date TEXT,
                discovered_date TEXT DEFAULT (datetime('now')),
                status TEXT DEFAULT 'new' CHECK(status IN ('new', 'filtered', 'ranked', 'summarized')),
                rank_score REAL,
                rank_reasoning TEXT,
                llm_summary TEXT,
                raw_data TEXT,
                UNIQUE(feed_name, topic, id)
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_entries_topic_status 
            ON entries(topic, status)
        ''')
        
        conn.commit()
        conn.close()
    
    def compute_entry_id(self, entry: Dict[str, Any]) -> str:
        """Generate a stable SHA-1 based ID for a feed entry."""
        candidate = entry.get("id") or entry.get("link")
        if candidate:
            parsed = urllib.parse.urlparse(candidate)
            candidate = urllib.parse.urlunparse(
                parsed._replace(query="", fragment="")
            )
            return hashlib.sha1(candidate.encode("utf-8")).hexdigest()

        parts = [
            entry.get("title", ""),
            entry.get("published", entry.get("updated", "")),
        ]
        concat = "||".join(parts)
        return hashlib.sha1(concat.encode("utf-8")).hexdigest()
    
    def is_new_entry(self, title: str) -> bool:
        """Check if an entry is new (title not in all_feed_entries.db)."""
        conn = sqlite3.connect(self.db_paths['all_feeds'])
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT 1 FROM feed_entries WHERE title = ?",
            (title,)
        )
        result = cursor.fetchone()
        conn.close()
        
        return result is None
    
    def save_feed_entry(self, entry: Dict[str, Any], feed_name: str, entry_id: str):
        """Save an entry to all_feed_entries.db with proper date formatting."""
        conn = sqlite3.connect(self.db_paths['all_feeds'])
        cursor = conn.cursor()
        
        authors = self._extract_authors(entry)
        raw_data = json.dumps(entry, default=str)
        
        # Ensure published_date is in YYYY-MM-DD format
        published_date = self._format_published_date(entry)
        title = entry.get('title', '').strip()
        
        cursor.execute('''
            INSERT OR REPLACE INTO feed_entries 
            (entry_id, feed_name, title, link, summary, authors, published_date, 
             first_seen, last_seen, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, 
                    COALESCE((SELECT first_seen FROM feed_entries WHERE title = ?), datetime('now')),
                    datetime('now'), ?)
        ''', (
            entry_id, feed_name, 
            title, 
            entry.get('link', ''),
            entry.get('summary', entry.get('description', '')),
            authors,
            published_date,
            title,  # for COALESCE subquery
            raw_data
        ))
        
        conn.commit()
        conn.close()
    
    def is_entry_in_history(self, entry_id: str) -> bool:
        """Check if an entry is already in matched_entries_history.db."""
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT 1 FROM matched_entries WHERE entry_id = ?",
            (entry_id,)
        )
        result = cursor.fetchone()
        conn.close()
        
        return result is not None
    
    def get_entry_topics_from_history(self, entry_id: str) -> List[str]:
        """Get the list of topics for an entry from matched_entries_history.db."""
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT topics FROM matched_entries WHERE entry_id = ?",
            (entry_id,)
        )
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0]:
            return [t.strip() for t in result[0].split(',')]
        return []
    
    def save_matched_entry(self, entry: Dict[str, Any], feed_name: str, topic: str, entry_id: str):
        """Save a matched entry to matched_entries_history.db, merging topics if entry already exists."""
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()
        
        # Check if entry already exists in history
        cursor.execute(
            "SELECT topics FROM matched_entries WHERE entry_id = ?",
            (entry_id,)
        )
        existing = cursor.fetchone()
        
        if existing:
            # Entry exists, merge the new topic with existing topics
            existing_topics = existing[0].split(', ') if existing[0] else []
            if topic not in existing_topics:
                existing_topics.append(topic)
                merged_topics = ', '.join(sorted(existing_topics))
                
                cursor.execute('''
                    UPDATE matched_entries 
                    SET topics = ?, matched_date = datetime('now')
                    WHERE entry_id = ?
                ''', (merged_topics, entry_id))
                
                logger.debug(f"Updated entry {entry_id[:8]}... with merged topics: {merged_topics}")
            else:
                logger.debug(f"Entry {entry_id[:8]}... already has topic '{topic}', skipping")
        else:
            # New entry, insert it
            authors = self._extract_authors(entry)
            raw_data = json.dumps(entry, default=str)
            published_date = self._format_published_date(entry)
            
            cursor.execute('''
                INSERT INTO matched_entries 
                (entry_id, feed_name, topics, title, link, summary, authors, 
                 published_date, matched_date, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            ''', (
                entry_id, feed_name, topic,
                entry.get('title', ''),
                entry.get('link', ''),
                entry.get('summary', entry.get('description', '')),
                authors,
                published_date,
                raw_data
            ))
            
            logger.debug(f"Added new entry {entry_id[:8]}... to history database with topic: {topic}")
        
        conn.commit()
        conn.close()
    
    def save_current_entry(self, entry: Dict[str, Any], feed_name: str, topic: str, entry_id: str):
        """Save an entry to papers.db for current run processing."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        
        authors = self._extract_authors(entry)
        raw_data = json.dumps(entry, default=str)
        published_date = self._format_published_date(entry)
        
        cursor.execute('''
            INSERT OR REPLACE INTO entries 
            (id, topic, feed_name, title, link, summary, authors, 
             published_date, discovered_date, status, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'filtered', ?)
        ''', (
            entry_id, topic, feed_name,
            entry.get('title', ''),
            entry.get('link', ''),
            entry.get('summary', entry.get('description', '')),
            authors,
            published_date,
            raw_data
        ))
        
        conn.commit()
        conn.close()
    
    def get_current_entries(self, topic: str = None, status: str = None) -> List[Dict[str, Any]]:
        """Get entries from papers.db with optional filtering."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        
        query = "SELECT * FROM entries WHERE 1=1"
        params = []
        
        if topic:
            query += " AND topic = ?"
            params.append(topic)
        
        if status:
            query += " AND status = ?"
            params.append(status)
        
        query += " ORDER BY discovered_date DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        # Convert to list of dicts
        columns = [description[0] for description in cursor.description]
        entries = [dict(zip(columns, row)) for row in rows]
        
        conn.close()
        return entries
    
    def get_entries_for_html_generation(self, topic: str) -> List[Dict[str, Any]]:
        """Get entries from papers.db organized by feed for HTML generation."""
        entries = self.get_current_entries(topic=topic, status='filtered')
        
        # Organize by feed
        entries_by_feed = {}
        for entry in entries:
            feed_name = entry.get('feed_name', 'unknown')
            if feed_name not in entries_by_feed:
                entries_by_feed[feed_name] = []
            entries_by_feed[feed_name].append(entry)
        
        return entries_by_feed
    
    def clear_current_db(self):
        """Clear the current run database."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        cursor.execute("DELETE FROM entries")
        conn.commit()
        conn.close()
    
    def purge_old_entries(self, days: int):
        """Remove entries from the most recent N days (including today) based on publication date (YYYY-MM-DD)."""
        start_date = (datetime.datetime.now().date() - datetime.timedelta(days=days - 1)).isoformat()
        end_date = datetime.datetime.now().date().isoformat()
        
        logger.info(f"Purging entries from {start_date} to {end_date} (last {days} days)")
        
        # Purge from all_feed_entries.db based on publication_date
        conn = sqlite3.connect(self.db_paths['all_feeds'])
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM feed_entries
            WHERE published_date IS NOT NULL
              AND TRIM(published_date) != ''
              AND DATE(published_date) BETWEEN DATE(?) AND DATE(?)
            """,
            (start_date, end_date),
        )
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(f"Purged {deleted_count} entries from all_feed_entries.db")
        
        # Purge from matched_entries_history.db based on published_date
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM matched_entries
            WHERE published_date IS NOT NULL
              AND TRIM(published_date) != ''
              AND DATE(published_date) BETWEEN DATE(?) AND DATE(?)
            """,
            (start_date, end_date),
        )
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(f"Purged {deleted_count} entries from matched_entries_history.db")
        
        # Purge from papers.db based on published_date
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM entries
            WHERE published_date IS NOT NULL
              AND TRIM(published_date) != ''
              AND DATE(published_date) BETWEEN DATE(?) AND DATE(?)
            """,
            (start_date, end_date),
        )
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(f"Purged {deleted_count} entries from papers.db")
    
    def _extract_authors(self, entry: Dict[str, Any]) -> str:
        """Extract authors string from entry."""
        authors = entry.get('authors', [])
        if authors:
            return ', '.join(author.get('name', '') for author in authors)
        return entry.get('author', '')
    
    def _format_published_date(self, entry: Dict[str, Any]) -> str:
        """Ensure published date is in YYYY-MM-DD format."""
        import time
        
        # Try to get parsed date first
        entry_published = entry.get('published_parsed') or entry.get('updated_parsed')
        if entry_published and isinstance(entry_published, time.struct_time):
            return datetime.date(*entry_published[:3]).isoformat()
        
        # Try string dates
        published_str = entry.get('published') or entry.get('updated', '')
        if published_str:
            try:
                # Try parsing common date formats
                for fmt in ['%Y-%m-%d', '%Y-%m-%dT%H:%M:%S%z', '%a, %d %b %Y %H:%M:%S %z']:
                    try:
                        dt = datetime.datetime.strptime(published_str[:19], fmt[:19])
                        return dt.date().isoformat()
                    except ValueError:
                        continue
                
                # If all parsing fails, try to extract YYYY-MM-DD if present
                import re
                match = re.search(r'(\d{4}-\d{2}-\d{2})', published_str)
                if match:
                    return match.group(1)
            except Exception:
                pass
        
        # Fallback to current date
        return datetime.date.today().isoformat()
    
    def close_all_connections(self):
        """Close any open database connections (placeholder for connection pooling)."""
        pass
