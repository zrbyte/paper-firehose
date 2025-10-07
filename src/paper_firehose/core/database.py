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
from typing import Dict, List, Any, Optional
import logging
import glob

from .paths import resolve_data_file

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages the three-database system for feed processing."""
    
    def __init__(self, config: Dict[str, Any]):
        """Resolve database file paths from config and ensure schemas exist."""
        self.config = config
        self.db_paths = {
            'all_feeds': str(resolve_data_file(config['database']['all_feeds_path'], ensure_parent=True)),
            'history': str(resolve_data_file(config['database']['history_path'], ensure_parent=True)),
            'current': str(resolve_data_file(config['database']['path'], ensure_parent=True)),
        }
        
        self._init_databases()
    
    def _init_databases(self):
        """Initialize all three databases with proper schemas."""
        # Initialize all_feed_entries.db
        self._init_all_feeds_db()
        
        # Initialize matched_entries_history.db
        self._init_history_db()
        
        # Initialize papers.db (current run)
        self._init_current_db()

    def _backup_sqlite(self, src_path: str, dest_path: str) -> None:
        """Create a consistent backup copy of a SQLite database.

        Creates a consistent copy of `src_path` at `dest_path` using the
        SQLite backup API. Overwrites any existing backup file at dest_path.
        """
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        # Use SQLite backup API for safety
        src_conn = sqlite3.connect(src_path)
        try:
            # Remove existing backup to avoid appending old pages
            if os.path.exists(dest_path):
                os.remove(dest_path)
            dest_conn = sqlite3.connect(dest_path)
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()

    def _rotate_backups(self, directory: str, stem: str, keep: int = 3) -> None:
        """Keep only the newest `keep` backups matching the given stem.

        Backup files are expected to match pattern: f"{stem}.YYYYMMDD-HHMMSS.backup.db".
        Older files beyond the `keep` most recent (by filename) are deleted.
        """
        pattern = os.path.join(directory, f"{stem}.*.backup.db")
        files = sorted(glob.glob(pattern))
        if len(files) <= keep:
            return
        to_delete = files[0 : len(files) - keep]
        for fp in to_delete:
            try:
                os.remove(fp)
                logger.info(f"Pruned old backup: {fp}")
            except Exception as e:
                logger.warning(f"Failed to remove old backup {fp}: {e}")

    def backup_important_databases(self) -> Dict[str, str]:
        """Backup history and all_feeds databases with timestamped rotation.

        - Writes timestamped backups alongside the source DBs in the runtime data directory.
        - Keeps up to 3 most recent backups per database, pruning older ones.

        Returns a dict mapping logical db keys to the created backup file paths.
        """
        backups = {}
        now = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        mappings = {
            'all_feeds': ('all_feed_entries', self.db_paths['all_feeds']),
            'history': ('matched_entries_history', self.db_paths['history']),
        }
        for key, (stem, path) in mappings.items():
            # Skip if source DB does not yet exist
            if not os.path.exists(path):
                continue
            directory = os.path.dirname(path)
            dest = os.path.join(directory, f"{stem}.{now}.backup.db")
            try:
                self._backup_sqlite(path, dest)
                backups[key] = dest
                logger.info(f"Backed up database '{key}' to {dest}")
                # Rotate: keep only newest 3
                self._rotate_backups(directory, stem, keep=3)
            except Exception as e:
                logger.error(f"Failed to backup database '{key}' from {path} to {dest}: {e}")
        return backups
    
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
        """Initialize the historical matches database.

        Creates the `matched_entries` table if missing. If present but
        missing required columns (abstract, doi, topics), it recreates the
        table for testing simplicity.
        """
        conn = sqlite3.connect(self.db_paths['history'])
        cursor = conn.cursor()

        # Inspect existing table
        cursor.execute("PRAGMA table_info(matched_entries)")
        info = cursor.fetchall()
        columns = {row[1] for row in info}

        required_columns = {
            'entry_id', 'feed_name', 'topics', 'title', 'link', 'summary',
            'authors', 'abstract', 'doi', 'published_date', 'matched_date', 'raw_data',
            'llm_summary', 'paper_qa_summary'
        }

        # If table exists, try lightweight migrations for new columns; otherwise recreate
        if len(columns) > 0:
            # Add new optional columns if missing (non-destructive)
            if 'llm_summary' not in columns:
                try:
                    cursor.execute("ALTER TABLE matched_entries ADD COLUMN llm_summary TEXT")
                    columns.add('llm_summary')
                except Exception:
                    pass
            if 'paper_qa_summary' not in columns:
                try:
                    cursor.execute("ALTER TABLE matched_entries ADD COLUMN paper_qa_summary TEXT")
                    columns.add('paper_qa_summary')
                except Exception:
                    pass

        need_recreate = (len(columns) == 0) or (not required_columns.issubset(columns))

        if need_recreate:
            cursor.execute('DROP TABLE IF EXISTS matched_entries')
            cursor.execute('''
                CREATE TABLE matched_entries (
                    entry_id TEXT PRIMARY KEY,
                    feed_name TEXT NOT NULL,
                    topics TEXT NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    summary TEXT,
                    authors TEXT,
                    abstract TEXT,
                    doi TEXT,
                    published_date TEXT,
                    matched_date TEXT DEFAULT (datetime('now')),
                    raw_data TEXT,
                    llm_summary TEXT,
                    paper_qa_summary TEXT
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
        """Initialize the current run processing database.

        Uses a composite primary key on (id, topic) so the same entry can
        exist once per topic.
        Creates the `entries` table if missing. If present but missing
        required columns (abstract, doi), it recreates the table for
        testing simplicity.
        """
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        
        # Inspect existing table
        cursor.execute("PRAGMA table_info(entries)")
        info = cursor.fetchall()
        columns = {row[1] for row in info}

        required_columns = {
            'id', 'topic', 'feed_name', 'title', 'link', 'summary', 'authors',
            'abstract', 'doi', 'published_date', 'discovered_date', 'status',
            'rank_score', 'rank_reasoning', 'llm_summary', 'raw_data'
        }
        need_recreate = (len(columns) == 0) or (not required_columns.issubset(columns))

        if need_recreate:
            cursor.execute('DROP TABLE IF EXISTS entries')
            cursor.execute('''
                CREATE TABLE entries (
                    id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    feed_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    summary TEXT,
                    authors TEXT,
                    abstract TEXT,
                    doi TEXT,
                    published_date TEXT,
                    discovered_date TEXT DEFAULT (datetime('now')),
                    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'filtered', 'ranked', 'summarized')),
                    rank_score REAL,
                    rank_reasoning TEXT,
                    llm_summary TEXT,
                    paper_qa_summary TEXT,
                    raw_data TEXT,
                    PRIMARY KEY (id, topic),
                    UNIQUE(feed_name, topic, id)
                )
            ''')
        else:
            # Lightweight migrations for new optional columns
            if 'paper_qa_summary' not in columns:
                try:
                    cursor.execute("ALTER TABLE entries ADD COLUMN paper_qa_summary TEXT")
                except Exception:
                    pass

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
    
    # Note: helper methods `is_entry_in_history` and `get_entry_topics_from_history`
    # were unused and have been removed to reduce surface area.
    
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
            doi = self._extract_doi(entry)
            
            cursor.execute('''
                INSERT INTO matched_entries 
                (entry_id, feed_name, topics, title, link, summary, authors, abstract, doi,
                 published_date, matched_date, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            ''', (
                entry_id, feed_name, topic,
                entry.get('title', ''),
                entry.get('link', ''),
                entry.get('summary', entry.get('description', '')),
                authors,
                None,  # abstract to be populated later (Crossref)
                doi,
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
        doi = self._extract_doi(entry)
        
        cursor.execute('''
            INSERT OR REPLACE INTO entries 
            (id, topic, feed_name, title, link, summary, authors, abstract, doi,
             published_date, discovered_date, status, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 'filtered', ?)
        ''', (
            entry_id, topic, feed_name,
            entry.get('title', ''),
            entry.get('link', ''),
            entry.get('summary', entry.get('description', '')),
            authors,
            None,  # abstract to be populated later (Crossref)
            doi,
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
    
    # Note: `get_entries_for_html_generation` has been removed; HTML generation
    # reads via `get_current_entries` directly.
    
    def clear_current_db(self):
        """Clear the current run database."""
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        cursor.execute("DELETE FROM entries")
        conn.commit()
        conn.close()

    def update_entry_rank(self, entry_id: str, topic: str, score: float | None, reasoning: str | None = None) -> None:
        """Update rank_score (and optionally rank_reasoning) for a single entry.

        Args:
            entry_id: Entry identifier (sha1 or normalized link id)
            topic: Topic name (composite key component)
            score: Rank score to persist (cosine similarity or None)
            reasoning: Optional concise reasoning string
        """
        conn = sqlite3.connect(self.db_paths['current'])
        cursor = conn.cursor()
        if reasoning is None:
            cursor.execute(
                "UPDATE entries SET rank_score = ? WHERE id = ? AND topic = ?",
                (score, entry_id, topic),
            )
        else:
            cursor.execute(
                "UPDATE entries SET rank_score = ?, rank_reasoning = ? WHERE id = ? AND topic = ?",
                (score, reasoning, entry_id, topic),
            )
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

    def _extract_doi(self, entry: Dict[str, Any]) -> Optional[str]:
        """Best-effort DOI extraction from common RSS fields.

        Enhanced to also scan text-bearing fields often used by publishers,
        including 'summary', 'summary_detail.value', and 'content[].value'.
        Returns a DOI string if found, otherwise None.
        """
        import re

        # DOI regex from Crossref guidelines (simplified)
        doi_re = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)

        def find_in_text(text: str | None) -> Optional[str]:
            """Search a text blob for a DOI pattern, stripping common prefixes first."""
            if not text:
                return None
            # Strip common prefixes like 'doi:'
            text = str(text).strip()
            if text.lower().startswith('doi:'):
                text = text[4:].strip()
            m = doi_re.search(text)
            return m.group(0) if m else None

        # Direct fields
        for key in ['doi', 'dc_identifier', 'dc:identifier', 'dc.identifier', 'dcIdentifier', 'prism:doi', 'prism_doi', 'guid']:
            doi = find_in_text(entry.get(key))
            if doi:
                return doi

        # Check id and link fields
        for key in ['id', 'link']:
            doi = find_in_text(entry.get(key))
            if doi:
                return doi

        # Check summary fields (plain and detailed)
        doi = find_in_text(entry.get('summary'))
        if doi:
            return doi
        summary_detail = entry.get('summary_detail') or {}
        if isinstance(summary_detail, dict):
            doi = find_in_text(summary_detail.get('value'))
            if doi:
                return doi
        # Some feeds use 'description' instead of 'summary'
        doi = find_in_text(entry.get('description'))
        if doi:
            return doi

        # Check content list for embedded DOI text
        contents = entry.get('content') or []
        if isinstance(contents, list):
            for c in contents:
                if isinstance(c, dict):
                    doi = find_in_text(c.get('value') or c.get('content'))
                    if doi:
                        return doi

        # Check links array for hrefs
        links = entry.get('links') or []
        if isinstance(links, list):
            for l in links:
                href = None
                if isinstance(l, dict):
                    href = l.get('href')
                else:
                    href = str(l)
                doi = find_in_text(href)
                if doi:
                    return doi

        return None
    
    def close_all_connections(self):
        """Close any open database connections (placeholder for connection pooling)."""
        pass
