"""
RSS feed processing functionality.
Fetches RSS feeds, applies regex filters, and manages entry storage.
"""

import feedparser
import feedparser.http
import io
import re
import time
import datetime
import urllib.parse
from typing import Dict, List, Any
import logging

import requests
import urllib3.exceptions

from ..core.database import DatabaseManager
from ..core.config import ConfigManager

logger = logging.getLogger(__name__)

# Default time window for processing entries (days); can be overridden by config.defaults.time_window_days
DEFAULT_TIME_WINDOW_DAYS = 365

# Per-feed HTTP timeout (seconds); can be overridden by config.defaults.feed_timeout_seconds.
# Bounds each read and the download as a whole.
DEFAULT_FEED_TIMEOUT_SECONDS = 30

# Largest feed body accepted (bytes, after decompression). The biggest
# configured feeds (Wiley, ~500 entries) are a few MB.
FEED_MAX_BYTES = 20 * 1024 * 1024


class FeedFetchError(Exception):
    """A feed could not be fetched or parsed."""


class FeedProcessor:
    """Processes RSS feeds with regex filtering and database storage."""

    def __init__(self, db_manager: DatabaseManager, config_manager: ConfigManager):
        """Bind database/config managers and derive the time window constraint."""
        self.db = db_manager
        self.config = config_manager
        # Resolve time window from config (defaults.time_window_days)
        cfg = self.config.load_config()
        defaults = cfg.get('defaults') or {}
        days = int(defaults.get('time_window_days', DEFAULT_TIME_WINDOW_DAYS))
        self.time_delta = datetime.timedelta(days=days)
        self.feed_timeout = float(defaults.get('feed_timeout_seconds', DEFAULT_FEED_TIMEOUT_SECONDS))
        self.feed_max_bytes = FEED_MAX_BYTES

        # One session for the whole run: it keeps cookies across redirects,
        # which publishers that answer with a cookie check need (Atypon
        # ``/action/showFeed`` feeds such as ChemRxiv). Same User-Agent and
        # Accept header as feedparser sends on its own.
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': feedparser.USER_AGENT,
            'Accept': feedparser.http.ACCEPT_HEADER,
        })

        # Feeds that could not be fetched or parsed during this run, so that
        # callers can tell "nothing new" from "could not read the feed".
        self.failed_feeds: List[Dict[str, Any]] = []

    def _fetch_feed(self, url: str) -> feedparser.FeedParserDict:
        """Fetch and parse one feed; raise :class:`FeedFetchError` if it cannot be read.

        HTTP(S) feeds go through ``self.session`` with a timeout; other URLs
        (``file://``, local paths) are handed to feedparser directly.
        """
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme in ('http', 'https'):
            body, status, final_url, headers = self._download(url)
            # Resolve relative links against the final URL after redirects.
            headers.setdefault('content-location', final_url)
            # Wrap the body in a stream: given bare bytes, feedparser first
            # tries open() on them as a file name, so a server answering with
            # a path would make us read that local file (issue #90).
            feed = feedparser.parse(io.BytesIO(body), response_headers=headers)
            feed['status'] = status
            feed['href'] = final_url
        else:
            feed = feedparser.parse(url)
            status = feed.get('status')
            if status is not None and status >= 400:
                raise FeedFetchError(f"HTTP {status}")

        if feed.bozo and not feed.entries:
            raise FeedFetchError(f"could not parse feed: {feed.get('bozo_exception')}")
        return feed

    def _download(self, url: str):
        """GET *url* through the session with bounded size and time.

        Returns ``(body, status_code, final_url, lowercased_headers)``. The
        body is streamed and the download is abandoned once it exceeds
        ``self.feed_max_bytes`` (counted after gzip/deflate decoding) or once
        ``self.feed_timeout`` seconds have passed in total. The per-read
        ``timeout`` alone would not stop a server that trickles bytes.

        Reads with urllib3's ``read1``, which returns whatever has arrived
        instead of waiting for a full chunk (``iter_content`` would block until
        64 KB or the end of a small body), so the deadline is checked as data
        trickles in.
        """
        deadline = time.monotonic() + self.feed_timeout
        try:
            with self.session.get(url, timeout=self.feed_timeout, stream=True) as response:
                if response.status_code >= 400:
                    raise FeedFetchError(f"HTTP {response.status_code} from {response.url}")
                chunks = []
                size = 0
                while True:
                    chunk = response.raw.read1(64 * 1024, decode_content=True)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.feed_max_bytes:
                        raise FeedFetchError(
                            f"response larger than {self.feed_max_bytes} bytes from {response.url}"
                        )
                    if time.monotonic() > deadline:
                        raise FeedFetchError(
                            f"download took longer than {self.feed_timeout:g} s from {response.url}"
                        )
                    chunks.append(chunk)
                headers = {k.lower(): v for k, v in response.headers.items()}
                return b"".join(chunks), response.status_code, response.url, headers
        except (requests.RequestException, urllib3.exceptions.HTTPError) as e:
            # urllib3 errors come from reading ``response.raw`` directly
            # (read timeouts, broken connections, bad gzip data).
            raise FeedFetchError(f"request failed: {e}") from e

    def _record_failure(self, feed_key: str, name: str, url: str, error: str) -> None:
        """Remember a feed that could not be read (once per feed per run)."""
        if any(f['feed'] == feed_key for f in self.failed_feeds):
            return
        self.failed_feeds.append({'feed': feed_key, 'name': name, 'url': url, 'error': error})

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
                try:
                    feed = self._fetch_feed(feed_url)
                except FeedFetchError as e:
                    logger.warning(f"Could not read feed '{feed_display_name}' ({feed_url}): {e}")
                    self._record_failure(feed_key, feed_display_name, feed_url, str(e))
                    new_entries_per_feed[feed_key] = []
                    continue
                if feed.bozo:
                    logger.warning(f"Feed '{feed_display_name}' has parsing issues: {feed.bozo_exception}")
                
                feed_entries = feed.entries
                logger.debug(f"Feed '{feed_display_name}' returned {len(feed_entries)} raw entries")
                feed_title = getattr(feed.feed, 'title', feed_display_name)
                
                # Add feed metadata to each entry
                for entry in feed_entries:
                    entry['feed_title'] = feed_title
                
                candidates = []

                for entry in feed_entries:
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
                    
                    candidates.append(entry)

                # Keep entries whose title is not in the dedup DB yet
                # (one batched lookup per feed).
                new_titles = self.db.filter_new_titles(
                    [e.get('title', '').strip() for e in candidates]
                )
                new_entries = [e for e in candidates if e.get('title', '').strip() in new_titles]
                for entry in new_entries:
                    logger.debug(f"New entry found: {entry.get('title', '').strip()[:50]}...")

                new_entries_per_feed[feed_key] = new_entries
                logger.info(f"Found {len(new_entries)} new entries in feed '{feed_display_name}'")

            except Exception as e:
                logger.error(f"Error processing feed '{feed_display_name}': {e}")
                self._record_failure(feed_key, feed_display_name, feed_url, f"processing error: {e}")
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
        items = []
        for feed_key, entries in all_entries_per_feed.items():
            display_name = enabled_feeds.get(feed_key, {}).get('name', feed_key)
            for entry in entries:
                items.append((entry, display_name, self.db.compute_entry_id(entry)))
        self.db.save_feed_entries(items)

        logger.info(f"Saved all processed entries to deduplication database")

    def prune_old_feed_entries(self) -> int:
        """Delete entries from all_feed_entries.db older than the configured time window.

        Keeps the dedup database bounded to the same age horizon used at
        ingestion (``defaults.time_window_days``, default 365 days), so entries
        older than that window are removed rather than accumulating forever.
        Returns the number of rows deleted.
        """
        return self.db.delete_all_feeds_older_than(self.time_delta.days)

