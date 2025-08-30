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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matched_entries (
                entry_id TEXT NOT NULL,
                feed_name TEXT NOT NULL,
                topic TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                summary TEXT,
                authors TEXT,
                published_date TEXT,
                matched_date TEXT DEFAULT (datetime('now')),
                raw_data TEXT,
                PRIMARY KEY (entry_id, feed_name, topic)
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_matched_entries_topic 
            ON matched_entries(topic)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_matched_entries_matched_date 
            ON matched_entries(matched_date)
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
    
    def is_new_entry(self, entry_id: str, feed_name: str) -> bool:
        """Check if an entry is new (not in all_feed_entries.db)."""
        conn = sqlite3.connect(self.db_paths['all_feeds'])
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT 1 FROM feed_entries WHERE entry_id = ? AND feed_name = ?",
            (entry_id, feed_name)
        )
        result = cursor.fetchone()
        conn.close()
        
        return result is None
    
    def save_feed_entry(self, entry: Dict[str, Any], feed_name: str, entry_id: str):
        """Save an entry to all_feed_entries.db."""
        conn = sqlite3.connect(self.db_paths['all_feeds'])
        cursor = conn.cursor()
        
        authors = self._extract_authors(entry)
        raw_data = json.dumps(entry, default=str)
        
        cursor.execute('''
            INSERT OR REPLACE INTO feed_entries 
            (entry_id, feed_name, title, link, summary, authors, published_date, 
             first_seen, last_seen, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, 
                    COALESCE((SELECT first_seen FROM feed_entries WHERE entry_id = ? AND feed_name = ?), datetime('now')),
                    datetime('now'), ?)
        ''', (
            entry_id, feed_name, 
            entry.get('title', ''), 
            entry.get('link', ''),
            entry.get('summary', entry.get('description', '')),
            authors,
            entry.get('published', entry.get('updated', '')),
            entry_id, feed_name,  # for COALESCE subquery
            raw_data
        ))
        
        conn.commit()
        conn.close()
    
    def save_matched_entry(self, entry: Dict[str, Any], feed_name: str, topic: str, entry_id: str):
        """Save a matched entry to matched_entries_history.db."""
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()
        
        authors = self._extract_authors(entry)
        raw_data = json.dumps(entry, default=str)
        
        cursor.execute('''
            INSERT OR IGNORE INTO matched_entries 
            (entry_id, feed_name, topic, title, link, summary, authors, 
             published_date, matched_date, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ''', (
            entry_id, feed_name, topic,
            entry.get('title', ''),
            entry.get('link', ''),
            entry.get('summary', entry.get('description', '')),
            authors,
            entry.get('published', entry.get('updated', '')),
            raw_data
        ))
        
        conn.commit()
        conn.close()
    
    def save_current_entry(self, entry: Dict[str, Any], feed_name: str, topic: str, entry_id: str):
        """Save an entry to papers.db for current run processing."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        
        authors = self._extract_authors(entry)
        raw_data = json.dumps(entry, default=str)
        
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
            entry.get('published', entry.get('updated', '')),
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
    
    def clear_current_db(self):
        """Clear the current run database."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        cursor.execute("DELETE FROM entries")
        conn.commit()
        conn.close()
    
    def purge_old_entries(self, days: int):
        """Remove entries older than specified days from all databases."""
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
        
        # Purge from all_feed_entries (keep recent for deduplication)
        if days < 120:  # Don't purge too aggressively from deduplication DB
            logger.warning(f"Not purging all_feed_entries.db - {days} days is too recent for deduplication")
        else:
            conn = sqlite3.connect(self.db_paths['all_feeds'])
            cursor = conn.cursor()
            cursor.execute("DELETE FROM feed_entries WHERE first_seen < ?", (cutoff,))
            conn.commit()
            conn.close()
            logger.info(f"Purged entries older than {days} days from all_feed_entries.db")
    
    def _extract_authors(self, entry: Dict[str, Any]) -> str:
        """Extract authors string from entry."""
        authors = entry.get('authors', [])
        if authors:
            return ', '.join(author.get('name', '') for author in authors)
        return entry.get('author', '')
    
    def close_all_connections(self):
        """Close any open database connections (placeholder for connection pooling)."""
        pass
