"""
Multi-source abstract fetcher with fallback logic.

Orchestrates fetching abstracts from multiple sources (Crossref, Semantic Scholar,
OpenAlex, PubMed) with intelligent fallback strategies based on journal/domain.
"""

from __future__ import annotations

import time
import json
import logging
from typing import Optional, Dict, Any, Iterable

import requests

from ..core.database import DatabaseManager
from ..core.apis import (
    get_crossref_abstract,
    search_crossref_abstract_by_title,
    get_semantic_scholar_abstract,
    get_openalex_abstract,
    get_pubmed_abstract_by_doi,
)
from ..core.doi_utils import extract_doi_from_json
from ..core.text_utils import clean_abstract_for_db
from ..core.abstract_source import AbstractSource, get_default_sources, get_biomedical_sources


logger = logging.getLogger(__name__)


def try_abstract_sources(
    sources: list[AbstractSource],
    doi: Optional[str],
    title: Optional[str],
    *,
    mailto: str,
    session: Optional[requests.Session]
) -> Optional[str]:
    """Try fetching abstract from a list of sources in order.

    Args:
        sources: List of AbstractSource instances to try in order
        doi: Digital Object Identifier (optional)
        title: Paper title (optional)
        mailto: Contact email for API calls
        session: requests.Session for API calls

    Returns:
        Abstract text or None if not found from any source
    """
    for source in sources:
        source_name = source.__class__.__name__
        try:
            result = source.fetch_abstract(
                doi=doi, title=title, mailto=mailto, session=session
            )
            if result:
                logger.debug(f"Abstract fetched successfully from {source_name}")
                return result
        except Exception as e:
            logger.warning(f"Failed to fetch abstract from {source_name}: {e}")
            # Continue to next source on error
            continue

    logger.debug(f"No abstract found from {len(sources)} sources (doi={doi}, title={title[:50] if title else None}...)")
    return None


def try_publisher_apis(
    doi: Optional[str],
    feed_name: str,
    link: str,
    *,
    mailto: str,
    session: Optional[requests.Session]
) -> Optional[str]:
    """Try publisher/aggregator APIs based on journal or domain.

    Order (by common coverage): Semantic Scholar, OpenAlex; for PNAS (or biomedical), try PubMed.

    Args:
        doi: Digital Object Identifier (optional)
        feed_name: Name of the RSS feed source
        link: URL to the paper
        mailto: Contact email for API calls
        session: requests.Session for API calls

    Returns:
        Abstract text or None if not found
    """
    fn = (feed_name or '').lower()
    domain = (link or '').lower()

    # Choose appropriate source list based on journal type
    if 'pnas' in fn or 'pnas.org' in domain:
        sources = get_biomedical_sources()
    else:
        sources = get_default_sources()

    return try_abstract_sources(sources, doi, None, mailto=mailto, session=session)


def iter_targets(
    db: DatabaseManager,
    topic: str,
    threshold: float
) -> Iterable[Dict[str, Any]]:
    """Yield ranked DB rows lacking abstracts for the given topic, highest score first.

    Args:
        db: DatabaseManager instance
        topic: Topic name to filter by
        threshold: Minimum rank score to include

    Yields:
        Dictionary representing each database row
    """
    # Use DatabaseManager's iter_targets method with additional abstract filtering
    for row in db.iter_targets(topic=topic, min_rank=threshold):
        # Filter out rows that already have abstracts
        abstract = row['abstract']
        if abstract is None or (isinstance(abstract, str) and abstract.strip() == ''):
            yield dict(row)


def fill_arxiv_summaries(
    db: DatabaseManager,
    topics: Optional[list[str]] = None
) -> int:
    """First pass: fill abstracts from summary for arXiv/cond-mat entries, no threshold.

    Args:
        db: DatabaseManager instance
        topics: Optional list of topics to process (None = all topics)

    Returns:
        Number of rows updated
    """
    with db.get_connection('current') as conn:
        cur = conn.cursor()
        params: list = []
        topic_filter = ""
        if topics:
            placeholders = ",".join(["?"] * len(topics))
            topic_filter = f" AND topic IN ({placeholders})"
            params.extend(topics)
        cur.execute(
            f"""
            SELECT id, topic, feed_name, link, summary
            FROM entries
            WHERE (abstract IS NULL OR TRIM(abstract) = '')
              AND (
                    LOWER(COALESCE(feed_name, '')) LIKE '%cond-mat%'
                 OR LOWER(COALESCE(feed_name, '')) LIKE '%arxiv%'
                 OR LOWER(COALESCE(link, '')) LIKE '%arxiv.org%'
              )
              {topic_filter}
            """,
            params,
        )
        rows = cur.fetchall()

    # Collect all updates for batch processing
    papers_updates = []
    history_updates = []

    for row in rows:
        id_ = row['id']
        tpc = row['topic']
        summary = row['summary']
        if not summary:
            continue
        cleaned = clean_abstract_for_db(summary)
        if cleaned:
            # Note: DOI stays None for these arXiv entries
            papers_updates.append((cleaned, None, id_, tpc))
            history_updates.append((cleaned, None, id_))

    # Batch update papers.db using DatabaseManager method
    if papers_updates:
        db.update_abstracts_batch(papers_updates)

    # Batch update history DB (best-effort)
    if history_updates:
        try:
            db.update_history_abstracts_batch(history_updates)
        except Exception as e:
            logger.warning(f"Failed to update history database in fill_arxiv_summaries: {e}", exc_info=True)

    return len(papers_updates)


def crossref_pass(
    db: DatabaseManager,
    topic: str,
    threshold: float,
    *,
    mailto: str,
    session: requests.Session,
    min_interval: float,
    max_per_topic: Optional[int],
    max_retries: int = 3
) -> int:
    """Second pass: Crossref only (DOI first, then title) for entries above threshold.

    Args:
        db: DatabaseManager instance
        topic: Topic name to process
        threshold: Minimum rank score to include
        mailto: Contact email for Crossref API
        session: requests.Session for API calls
        min_interval: Minimum seconds between API calls
        max_per_topic: Optional maximum fetches per topic
        max_retries: Maximum retry attempts for failed requests

    Returns:
        Number of abstracts fetched
    """
    # Collect all updates for batch processing
    papers_updates = []
    history_updates = []

    fetched = 0
    for row in iter_targets(db, topic, threshold):
        doi = row.get('doi') or extract_doi_from_json(row.get('raw_data'))
        if not doi:
            try:
                raw = row.get('raw_data')
                if raw:
                    obj = json.loads(raw)
                    doi = obj.get('arxiv_doi') or obj.get('arXiv_doi')
            except Exception as e:
                logger.debug(f"Failed to extract arXiv DOI from raw_data for entry {row.get('id')}: {e}")
        abstract: Optional[str] = None
        if doi:
            abstract = get_crossref_abstract(doi, mailto=mailto, session=session, max_retries=max_retries)
        time.sleep(min_interval)
        if not abstract:
            abstract = search_crossref_abstract_by_title(row.get('title') or '', mailto=mailto, session=session, max_retries=max_retries)
            time.sleep(min_interval)
        if abstract:
            abstract = clean_abstract_for_db(abstract)
            papers_updates.append((abstract, doi, row['id'], topic))
            history_updates.append((abstract, doi, row['id']))
            fetched += 1
            if max_per_topic is not None and fetched >= max_per_topic:
                break

    # Batch update papers.db using DatabaseManager method
    if papers_updates:
        db.update_abstracts_batch(papers_updates)

    # Batch update history DB (best-effort)
    if history_updates:
        try:
            db.update_history_abstracts_batch(history_updates)
        except Exception as e:
            logger.warning(f"Failed to update history database: {e}", exc_info=True)

    return fetched


def fallback_pass(
    db: DatabaseManager,
    topic: str,
    threshold: float,
    *,
    mailto: str,
    session: requests.Session,
    min_interval: float,
    max_per_topic: Optional[int]
) -> int:
    """Third pass: remaining above-threshold entries → Semantic Scholar / OpenAlex / PubMed.

    Args:
        db: DatabaseManager instance
        topic: Topic name to process
        threshold: Minimum rank score to include
        mailto: Contact email for API calls
        session: requests.Session for API calls
        min_interval: Minimum seconds between API calls
        max_per_topic: Optional maximum fetches per topic

    Returns:
        Number of abstracts fetched
    """
    # Collect all updates for batch processing
    papers_updates = []
    history_updates = []

    fetched = 0
    for row in iter_targets(db, topic, threshold):
        # Skip rows already filled by previous passes
        # iter_targets already filters abstract IS NULL or empty
        doi = row.get('doi') or extract_doi_from_json(row.get('raw_data'))
        if not doi:
            try:
                raw = row.get('raw_data')
                if raw:
                    obj = json.loads(raw)
                    doi = obj.get('arxiv_doi') or obj.get('arXiv_doi')
            except Exception as e:
                logger.debug(f"Failed to extract arXiv DOI from raw_data for entry {row.get('id')}: {e}")
        abstract = try_publisher_apis(doi, row.get('feed_name') or '', row.get('link') or '', mailto=mailto, session=session)
        if abstract:
            abstract = clean_abstract_for_db(abstract)
            papers_updates.append((abstract, doi, row['id'], topic))
            history_updates.append((abstract, doi, row['id']))
            fetched += 1
            time.sleep(min_interval)
            if max_per_topic is not None and fetched >= max_per_topic:
                break

    # Batch update papers.db using DatabaseManager method
    if papers_updates:
        db.update_abstracts_batch(papers_updates)

    # Batch update history DB (best-effort)
    if history_updates:
        try:
            db.update_history_abstracts_batch(history_updates)
        except Exception as e:
            logger.warning(f"Failed to update history database: {e}", exc_info=True)

    return fetched
