"""
Data models for the paper firehose system.
"""

from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from datetime import datetime


@dataclass
class FeedEntry:
    """Represents a single RSS feed entry."""
    entry_id: str
    feed_name: str
    title: str
    link: str
    summary: Optional[str] = None
    authors: Optional[str] = None
    published_date: Optional[str] = None
    discovered_date: Optional[datetime] = None
    raw_data: Optional[Dict[str, Any]] = None


@dataclass
class FilteredEntry(FeedEntry):
    """Represents an entry that has passed filtering."""
    topic: str
    status: str = 'filtered'
    rank_score: Optional[float] = None
    rank_reasoning: Optional[str] = None
    llm_summary: Optional[str] = None


@dataclass
class TopicConfig:
    """Configuration for a specific topic."""
    name: str
    description: str
    feeds: List[str]
    filter_pattern: str
    filter_fields: List[str]
    ranking_prompt: Optional[str] = None
    ranking_top_n: int = 5
    output_filename: str = ""
    output_template: str = "topic_summary.html"
    archive: bool = False


@dataclass
class ProcessingRun:
    """Represents a processing run for tracking and debugging."""
    run_id: str
    command: str
    topic: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: str = 'running'
    entries_processed: int = 0
    error_message: Optional[str] = None
