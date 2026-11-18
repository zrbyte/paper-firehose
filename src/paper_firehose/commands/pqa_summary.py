"""
PDF full-text summaries
=======================

This command selects entries from papers.db for a given topic with
``rank_score >=`` the configured threshold and downloads arXiv PDFs for them,
adhering to arXiv API Terms of Use (polite rate limiting and descriptive
``User-Agent`` with contact email).

Workflow
--------

1. Load configuration and identify ranked entries above threshold
2. Download arXiv PDFs (respecting rate limits, reusing archived copies)
3. With ``summarize=True`` (CLI ``--summarize``): extract each PDF's text and
   summarize it with one OpenAI call (``processors/pdf_summarizer.py``)
4. Write ``{"summary", "methods"}`` JSON into the ``paper_qa_summary`` column of
   papers.db and matched_entries_history.db

The command and the ``paperqa:`` topic section keep their historical names;
paper-qa itself is no longer used.
"""

from __future__ import annotations

import os
import re
import json
import time
import shutil
import logging
import sqlite3
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

import requests
import feedparser

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.command_utils import resolve_topics
from ..core.paths import resolve_data_path
from ..processors.pdf_summarizer import PdfSummarizer, build_instructions

logger = logging.getLogger(__name__)

DEFAULT_RPS = 0.3
DEFAULT_MAX_RETRIES = 3


ARXIV_API = "https://export.arxiv.org/api/query"


def _get_topic_paperqa_config(topic_cfg: Dict[str, Any], topic_name: str) -> Dict[str, Any]:
    """Extract paperqa config from topic config.

    Args:
        topic_cfg: Topic config dict (must have 'paperqa' key)
        topic_name: Topic name for logging

    Returns:
        Topic's paperqa config dict

    Raises:
        ValueError: If topic has no paperqa section
    """
    topic_pqa = topic_cfg.get('paperqa')
    if not topic_pqa:
        raise ValueError(
            f"Topic '{topic_name}' missing required 'paperqa' section. "
            "All topics must define PDF summary settings under 'paperqa'."
        )

    logger.info("Loaded paperqa config for topic '%s'", topic_name)
    return topic_pqa


def _ensure_dirs(download_dir: str, archive_dir: str) -> None:
    """Ensure the working download/archive directories exist before use."""
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)


def _resolve_mailto(config: Dict[str, Any]) -> str:
    """Resolve contact email for API requests from config/env fallbacks."""
    # Reuse abstracts mailto if available; otherwise fallback to env or placeholder
    defaults = (config.get('defaults') or {})
    abstracts = (defaults.get('abstracts') or {})
    mailto = abstracts.get('mailto') or os.environ.get('MAILTO') or 'you@example.org'
    return str(mailto)


def _arxiv_user_agent(mailto: str) -> str:
    """Build an arXiv-compliant User-Agent string with contact information."""
    # Include contact per arXiv API guidance
    return f"paper-firehose/pqa-summary (+mailto:{mailto})"


def _iter_ranked_entries(db: DatabaseManager, topic: str, min_rank: float) -> List[Dict[str, Any]]:
    """Fetch ranked DB rows for a topic sorted descending by score."""
    rows = db.get_entries_by_criteria(
        topic=topic,
        min_rank=float(min_rank),
        order_by='rank_score DESC'
    )
    return [dict(row) for row in rows]


def _fetch_history_entries_by_ids(db: DatabaseManager, entry_ids: List[str], *, matched_date: Optional[str] = None, feed_like: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch rows from matched_entries_history.db for the given entry_ids.

    Optional filters:
    - matched_date: 'YYYY-MM-DD' exact match on the date portion
    - feed_like: substring to match in feed_name (case-insensitive)
    """
    if not entry_ids:
        return []

    with db.get_connection('history') as conn:
        cur = conn.cursor()
        # Build query
        base = (
            "SELECT entry_id, feed_name, topics, title, link, summary, doi, matched_date "
            "FROM matched_entries WHERE entry_id IN ({placeholders})"
        )
        placeholders = ",".join(["?"] * len(entry_ids))
        q = base.format(placeholders=placeholders)
        params: List[Any] = list(entry_ids)
        # Date restriction (match date part of matched_date)
        if matched_date:
            q += " AND date(matched_date) = date(?)"
            params.append(matched_date)
        # Feed substring match
        if feed_like:
            q += " AND LOWER(feed_name) LIKE ?"
            params.append(f"%{feed_like.lower()}%")
        # Order by the order of input ids (best-effort): fetch then sort in Python
        cur.execute(q, params)
        rows = [dict(row) for row in cur.fetchall()]

    # Preserve input order
    idx = {eid: i for i, eid in enumerate(entry_ids)}
    rows.sort(key=lambda r: idx.get(r.get('entry_id', ''), 1e9))
    return rows


_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")


def _extract_arxiv_id_from_link(link: str | None) -> Optional[str]:
    """Pull an arXiv identifier out of common arXiv link patterns."""
    if not link:
        return None
    try:
        if 'arxiv.org' not in link:
            return None
        # Common patterns: /abs/<id>(vN), /pdf/<id>(vN).pdf
        m = re.search(r"/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", link)
        if m:
            return m.group(1)
    except (TypeError, AttributeError):
        return None
    return None


def _extract_arxiv_id_from_doi(doi: str | None) -> Optional[str]:
    """Derive an arXiv identifier from the canonical arXiv DOI form."""
    if not doi:
        return None
    # Crossref canonical DOI for arXiv looks like: 10.48550/arXiv.2509.09390
    try:
        doi_l = doi.lower().strip()
        if doi_l.startswith("10.48550/arxiv."):
            match = re.search(r"arxiv\.(.+)$", doi, flags=re.IGNORECASE)
            if match:
                return match.group(1)
            parts = doi.split("/", 1)
            if len(parts) > 1:
                return parts[1]
    except (TypeError, AttributeError):
        return None
    return None


def _extract_arxiv_id_from_text(text: str | None) -> Optional[str]:
    """Scan arbitrary text for arXiv-style identifier tokens."""
    if not text:
        return None
    # Capture e.g. arXiv:2509.09390v1 or bare 2509.09390
    try:
        # First try arXiv:<id>
        m = re.search(r"arXiv:([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", text)
        if m:
            return m.group(1)
        # Then try any id-like token
        m2 = _ARXIV_ID_RE.search(text)
        if m2:
            return m2.group(1) + (m2.group(2) or "")
    except (TypeError, AttributeError):
        return None
    return None


def _resolve_arxiv_id(entry: Dict[str, Any]) -> Optional[str]:
    """Best-effort arXiv ID detection using link, DOI, summary, then title."""
    # Priority: link -> doi -> summary -> title
    arx = _extract_arxiv_id_from_link(entry.get('link'))
    if arx:
        return arx
    arx = _extract_arxiv_id_from_doi(entry.get('doi'))
    if arx:
        return arx
    arx = _extract_arxiv_id_from_text(entry.get('summary'))
    if arx:
        return arx
    arx = _extract_arxiv_id_from_text(entry.get('title'))
    return arx


def _find_archived_pdf(archive_dir: str, arxiv_id: str) -> Optional[str]:
    """Find an archived PDF matching arXiv ID, tolerant to version suffix presence/absence.

    Tries the following in order:
    - exact: <id>.pdf
    - without version: <id_wo_v>.pdf
    - any file starting with <id_wo_v> (e.g., 2401.17779v2.pdf)
    """
    base_id = arxiv_id
    # split off version suffix if present
    m = re.match(r"^(\d{4}\.\d{4,5})(v\d+)?$", arxiv_id)
    if m:
        base_id = m.group(1)
        version = m.group(2)
    else:
        version = None

    # exact
    p_exact = os.path.join(archive_dir, f"{arxiv_id}.pdf")
    if os.path.exists(p_exact):
        return p_exact

    # without version
    p_base = os.path.join(archive_dir, f"{base_id}.pdf")
    if os.path.exists(p_base):
        return p_base

    # any starting with base id
    try:
        for fn in os.listdir(archive_dir):
            if fn.lower().endswith('.pdf') and fn.startswith(base_id):
                return os.path.join(archive_dir, fn)
    except FileNotFoundError:
        pass
    return None


def _query_arxiv_api_for_pdf(arxiv_id: str, *, mailto: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """Return direct PDF link for arXiv ID by querying the API.

    Adheres to arXiv API guidance by using export.arxiv.org with a
    descriptive User-Agent including contact email.
    """
    sess = session or requests.Session()
    headers = {"User-Agent": _arxiv_user_agent(mailto)}
    url = f"{ARXIV_API}?id_list={arxiv_id}"
    try:
        r = sess.get(url, headers=headers, timeout=20)
        # Honor Retry-After on 429/5xx
        if r.status_code in (429, 500, 502, 503, 504):
            ra = r.headers.get("Retry-After")
            if ra:
                try:
                    time.sleep(max(1.0, float(ra)))
                except (ValueError, TypeError):
                    time.sleep(3.0)
            else:
                time.sleep(3.0)
            return None
        r.raise_for_status()
        feed = feedparser.parse(r.text)
        if not feed.entries:
            return None
        entry = feed.entries[0]
        for l in entry.get('links', []):
            # Prefer explicit PDF link
            if l.get('type') == 'application/pdf':
                href = l.get('href')
                if href:
                    return href
        # Fallback: construct PDF URL if API didn’t provide a PDF link
        # Preserve version suffix when present
        arxiv_id_clean = arxiv_id
        return f"https://arxiv.org/pdf/{arxiv_id_clean}.pdf"
    except Exception as e:
        logger.debug(f"arXiv API query failed for {arxiv_id}: {e}")
        return None


def _download_pdf(pdf_url: str, dest_path: str, *, mailto: str, session: Optional[requests.Session] = None, max_retries: int = 3) -> bool:
    """Download a PDF with polite retry/backoff behavior; returns True on success."""
    sess = session or requests.Session()
    headers = {"User-Agent": _arxiv_user_agent(mailto)}
    backoff = 1.0
    for attempt in range(max_retries):
        try:
            with sess.get(pdf_url, headers=headers, timeout=60, stream=True) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    ra = r.headers.get('Retry-After')
                    if ra:
                        try:
                            wait = float(ra)
                        except (ValueError, TypeError):
                            wait = 3.0
                    else:
                        wait = backoff
                    time.sleep(max(3.0, wait))
                    backoff = min(16.0, backoff * 2)
                    continue
                r.raise_for_status()
                total = 0
                with open(dest_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
                # Basic sanity check: non-trivial size
                return total > 10_000
        except Exception as e:
            logger.debug(f"Download attempt {attempt+1} failed for {pdf_url}: {e}")
            time.sleep(backoff)
            backoff = min(16.0, backoff * 2)
    return False


def _move_to_archive(paths: List[str], archive_dir: str) -> None:
    """Move downloaded PDFs into the archive directory, avoiding overwrite collisions."""
    for p in paths:
        try:
            if not os.path.exists(p):
                continue
            base = os.path.basename(p)
            dest = os.path.join(archive_dir, base)
            # Avoid overwrite: append numeric suffix if needed
            if os.path.exists(dest):
                stem, ext = os.path.splitext(base)
                i = 1
                while os.path.exists(os.path.join(archive_dir, f"{stem}.{i}{ext}")):
                    i += 1
                dest = os.path.join(archive_dir, f"{stem}.{i}{ext}")
            shutil.move(p, dest)
        except OSError as e:
            logger.warning(f"Failed to move {p} to archive: {e}")


def _cleanup_archive(archive_dir: str, *, max_age_days: int = 30) -> None:
    """Remove archived PDFs older than max_age_days."""
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    removed = 0
    try:
        entries = os.listdir(archive_dir)
    except FileNotFoundError:
        return
    for fn in entries:
        if not fn.lower().endswith('.pdf'):
            continue
        path = os.path.join(archive_dir, fn)
        try:
            if not os.path.isfile(path):
                continue
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("Failed to remove archived PDF %s: %s", path, e)
    if removed:
        logger.info("Removed %d archived PDFs older than %d days", removed, max_age_days)


def _lookup_entry_id_by_arxiv(db: DatabaseManager, arxiv_id: str) -> Optional[str]:
    """Look up an entry_id in the history DB by arXiv ID (matching the link column)."""
    # Strip version suffix for matching (e.g. 2510.13641v2 -> 2510.13641)
    base_id = re.sub(r'v\d+$', '', arxiv_id)
    patterns = [f"%arxiv.org/abs/{base_id}%", f"%arxiv.org/pdf/{base_id}%"]
    try:
        with db.get_connection('history', row_factory=True) as conn:
            for pat in patterns:
                row = conn.execute(
                    "SELECT entry_id FROM matched_entries WHERE link LIKE ? LIMIT 1",
                    (pat,),
                ).fetchone()
                if row:
                    logger.debug("Resolved arXiv:%s -> entry_id=%s via history DB", arxiv_id, row['entry_id'])
                    return row['entry_id']
    except sqlite3.Error as e:
        logger.debug("Failed to look up arXiv:%s in history: %s", arxiv_id, e)
    return None


def _write_pqa_summary_to_dbs(db: DatabaseManager, entry_id: str, json_summary: str, *, topic: Optional[str] = None) -> None:
    """Write paper_qa_summary JSON into both current and history DBs.

    - papers.db: update the row for (id, topic) when topic is provided; otherwise update all rows with id = entry_id.
    - matched_entries_history.db: update row with entry_id
    """
    # Current DB
    try:
        with db.get_connection('current', row_factory=False) as conn:
            cur = conn.cursor()
            if topic:
                cur.execute(
                    "UPDATE entries SET paper_qa_summary = ? WHERE id = ? AND topic = ?",
                    (json_summary, entry_id, topic),
                )
            else:
                cur.execute(
                    "UPDATE entries SET paper_qa_summary = ? WHERE id = ?",
                    (json_summary, entry_id),
                )
            updated_current = cur.rowcount
            if topic:
                logger.info(
                    "Summary DB write (papers.db): entry_id=%s topic=%s updated_rows=%d",
                    entry_id,
                    topic,
                    updated_current,
                )
            else:
                logger.info(
                    "Summary DB write (papers.db): entry_id=%s updated_rows=%d",
                    entry_id,
                    updated_current,
                )
    except sqlite3.Error as e:
        logger.debug("Failed to write to papers.db for %s: %s", entry_id, e)
    # History DB
    try:
        with db.get_connection('history', row_factory=False) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matched_entries SET paper_qa_summary = ? WHERE entry_id = ?", (json_summary, entry_id))
            updated_history = cur.rowcount
            logger.info("Summary DB write (history.db): entry_id=%s updated_rows=%d", entry_id, updated_history)
    except sqlite3.Error as e:
        logger.debug("Failed to write to matched_entries_history.db for %s: %s", entry_id, e)


def _normalize_arxiv_arg(arg: str) -> Optional[str]:
    """Accept an arXiv URL or bare ID and return a normalized ID (with version if present)."""
    if not arg:
        return None
    arg = arg.strip()
    # URL case
    if 'arxiv.org' in arg:
        return _extract_arxiv_id_from_link(arg)
    # Bare ID case
    m = _ARXIV_ID_RE.fullmatch(arg) or re.match(r"^[0-9]{4}\.[0-9]{4,5}(?:v\d+)?$", arg)
    if m:
        return arg
    # arXiv:ID case
    if arg.lower().startswith('arxiv:'):
        return _extract_arxiv_id_from_text(arg)
    return None


def _summarize_targets(
    db: DatabaseManager,
    cfg_mgr: ConfigManager,
    targets: List[Tuple[Optional[str], str, str, Optional[str]]],
    topic_cfg_cache: Dict[str, Dict[str, Any]],
) -> int:
    """Summarize (entry_id, arxiv_id, pdf_path, topic) targets grouped by topic; return count written."""
    by_topic: Dict[Optional[str], List[Tuple[Optional[str], str, str, Optional[str]]]] = defaultdict(list)
    for target in targets:
        by_topic[target[3]].append(target)

    written = 0
    for topic_name, topic_targets in by_topic.items():
        if not topic_name:
            logger.warning("Skipping %d PDFs with no topic context", len(topic_targets))
            continue
        try:
            topic_cfg = topic_cfg_cache.get(topic_name) or cfg_mgr.load_topic_config(topic_name)
            paperqa_cfg = _get_topic_paperqa_config(topic_cfg, topic_name)
        except Exception as e:
            logger.error("Failed to load paperqa config for '%s': %s. Skipping.", topic_name, e)
            continue

        try:
            summarizer = PdfSummarizer(
                paperqa_cfg.get('llm'),
                reasoning_effort=paperqa_cfg.get('reasoning_effort'),
            )
        except Exception as e:
            logger.error("Cannot create OpenAI client (is OPENAI_API_KEY set?): %s", e)
            return written

        ranking_query = (topic_cfg.get('ranking') or {}).get('query')
        instructions = build_instructions(paperqa_cfg.get('prompt'), ranking_query)
        logger.info("Summarizing %d PDFs for topic '%s' with model=%s", len(topic_targets), topic_name, summarizer.model)

        for eid, aid, pdf_path, tctx in topic_targets:
            result = summarizer.summarize_pdf(pdf_path, instructions)
            if not result:
                logger.warning("No summary produced for arXiv:%s (entry_id=%s)", aid, eid or "-")
                continue
            logger.info("Summary for arXiv:%s (entry_id=%s): %s", aid, eid or "-", result['summary'][:300])

            if not eid and aid:
                # Manual --arxiv mode: look up entry in history DB by arXiv link
                eid = _lookup_entry_id_by_arxiv(db, aid)
            if eid:
                _write_pqa_summary_to_dbs(db, eid, json.dumps(result, ensure_ascii=False), topic=tctx)
                written += 1
            else:
                logger.warning("Got summary for arXiv:%s but no matching entry in DB; not stored", aid)
    return written


def run(
    config_path: str,
    topic: Optional[str] = None,
    *,
    rps: Optional[float] = None,
    limit: Optional[int] = None,
    arxiv: Optional[List[str]] = None,
    entry_ids: Optional[List[str]] = None,
    use_history: bool = False,
    history_date: Optional[str] = None,
    history_feed_like: Optional[str] = None,
    summarize: bool = False,
) -> None:
    """Download arXiv PDFs for ranked entries and optionally summarize them.

    Workflow overview
    -----------------

    - Load configuration/database state and prepare download/archive folders.
    - Determine targets either from ranked topic entries (respecting the download
      rank threshold and optional ``limit``) or from the explicit ``arxiv``/``entry_ids``
      arguments, optionally pulling metadata from the history database when
      ``use_history`` is enabled.
    - Resolve arXiv IDs, reuse archived PDFs when possible, download missing
      PDFs under the configured rate limit, and archive successful downloads.
    - When ``summarize`` is True, summarize each PDF with one OpenAI call using
      the topic's ``paperqa.llm``/``prompt`` and write the JSON result back to
      both ``papers.db`` and ``matched_entries_history.db`` when an
      ``entry_id`` is available.
    - ``rps`` overrides the per-topic (or default) arXiv request rate.
    """
    download_dir = str(resolve_data_path('paperqa'))
    archive_dir = str(resolve_data_path('paperqa_archive'))
    cfg_mgr = ConfigManager(config_path)
    if not cfg_mgr.validate_config():
        logger.error("Configuration validation failed")
        _cleanup_archive(archive_dir)
        return
    config = cfg_mgr.load_config()
    db = DatabaseManager(config)

    _ensure_dirs(download_dir, archive_dir)

    topics = resolve_topics(cfg_mgr, topic)
    mailto = _resolve_mailto(config)

    downloaded_paths: List[str] = []
    summarize_targets: List[Tuple[Optional[str], str, str, Optional[str]]] = []  # (entry_id, arxiv_id, pdf_path, topic_ctx)
    topic_cfg_cache: Dict[str, Dict[str, Any]] = {}  # topic name -> loaded topic config
    sess = requests.Session()

    total_candidates = 0
    total_downloaded = 0
    # Minimum 3 s between arXiv requests per ToU, whatever rps is configured
    min_interval_default = max(3.0, 1.0 / max(rps or DEFAULT_RPS, 0.01))

    # Manual mode: download specific arXiv IDs/URLs if provided
    if arxiv:

        # Resolve topic context for summarization: explicit --topic, or first available topic
        manual_topic_ctx: Optional[str] = topic or (topics[0] if topics else None)

        ids: List[str] = []
        for a in arxiv:
            nid = _normalize_arxiv_arg(a)
            if nid:
                ids.append(nid)
            else:
                logger.warning("Ignoring invalid --arxiv value: %s", a)
        logger.info("Manual arXiv list: %d item(s)", len(ids))
        for arxiv_id in ids:
            fname_id = arxiv_id
            fname = f"{fname_id.replace('/', '_')}.pdf"
            dest_path = os.path.join(download_dir, fname)
            # Prefer existing archived file (any matching pattern)
            archived_path = _find_archived_pdf(archive_dir, arxiv_id)
            if archived_path:
                logger.debug("Using archived PDF for %s: %s", arxiv_id, archived_path)
                summarize_targets.append((None, arxiv_id, archived_path, manual_topic_ctx))
                continue
            if os.path.exists(dest_path):
                logger.debug("Skipping %s (already downloaded)", arxiv_id)
                downloaded_paths.append(dest_path)
                summarize_targets.append((None, arxiv_id, dest_path, manual_topic_ctx))
                continue
            pdf_url = _query_arxiv_api_for_pdf(arxiv_id, mailto=mailto, session=sess) or f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=DEFAULT_MAX_RETRIES)
            if ok:
                downloaded_paths.append(dest_path)
                summarize_targets.append((None, arxiv_id, dest_path, manual_topic_ctx))
                total_downloaded += 1
                logger.info("Downloaded arXiv PDF: %s -> %s", arxiv_id, dest_path)
            else:
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except OSError:
                    pass
                logger.warning("Failed to download PDF for arXiv:%s", arxiv_id)
            time.sleep(min_interval_default)

        _move_to_archive(downloaded_paths, archive_dir)
        # Replace any targets that were in download_dir with archive paths
        repaired: List[Tuple[Optional[str], str, str, Optional[str]]] = []
        for eid, aid, path, tctx in summarize_targets:
            if os.path.dirname(path) == os.path.abspath(download_dir) or path.startswith(download_dir):
                ap = _find_archived_pdf(archive_dir, aid) or path
                repaired.append((eid, aid, ap, tctx))
            else:
                repaired.append((eid, aid, path, tctx))
        summarize_targets = repaired
        logger.info("Completed pqa_summary (manual): downloaded=%d, archived=%d", total_downloaded, len(downloaded_paths))
        # Fall through to optional summarization below

    # History-by-IDs mode: look up entries in history DB by entry_id and download
    if entry_ids:

        rows = _fetch_history_entries_by_ids(db, entry_ids, matched_date=history_date, feed_like=history_feed_like if use_history else None)
        logger.info("History lookup: requested=%d, found=%d (date=%s, feed~%s)", len(entry_ids), len(rows), history_date or '-', history_feed_like or '-')
        for row in rows:
            # Determine topic context for relevance prompt
            topic_ctx: Optional[str] = None
            if topic:
                topic_ctx = topic
            else:
                topics_csv = (row.get('topics') or '').strip()
                if topics_csv:
                    topic_ctx = topics_csv.split(',')[0].strip()
            arxiv_id = _resolve_arxiv_id({
                'link': row.get('link'),
                'doi': row.get('doi'),
                'summary': row.get('summary'),
                'title': row.get('title'),
            })
            if not arxiv_id:
                logger.warning("No arXiv ID detected for entry_id=%s", row.get('entry_id'))
                continue
            fname = f"{arxiv_id.replace('/', '_')}.pdf"
            dest_path = os.path.join(download_dir, fname)
            # Prefer existing archived file (any matching pattern)
            archived_match = _find_archived_pdf(archive_dir, arxiv_id)
            if archived_match:
                logger.debug("Using archived PDF for %s: %s", arxiv_id, archived_match)
                summarize_targets.append((row.get('entry_id'), arxiv_id, archived_match, topic_ctx))
                continue
            if os.path.exists(dest_path):
                logger.debug("Skipping %s (already downloaded)", arxiv_id)
                downloaded_paths.append(dest_path)
                summarize_targets.append((row.get('entry_id'), arxiv_id, dest_path, topic_ctx))
                continue
            pdf_url = _query_arxiv_api_for_pdf(arxiv_id, mailto=mailto, session=sess) or f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=DEFAULT_MAX_RETRIES)
            if ok:
                downloaded_paths.append(dest_path)
                summarize_targets.append((row.get('entry_id'), arxiv_id, dest_path, topic_ctx))
                total_downloaded += 1
                logger.info("Downloaded arXiv PDF: %s -> %s (entry_id=%s)", arxiv_id, dest_path, row.get('entry_id'))
            else:
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except OSError:
                    pass
                logger.warning("Failed to download PDF for arXiv:%s (entry_id=%s)", arxiv_id, row.get('entry_id'))
            time.sleep(min_interval_default)

        _move_to_archive(downloaded_paths, archive_dir)
        # Repair target paths to point at archive if needed
        repaired_h: List[Tuple[Optional[str], str, str, Optional[str]]] = []
        for eid, aid, path, tctx in summarize_targets:
            ap = _find_archived_pdf(archive_dir, aid) or path
            repaired_h.append((eid, aid, ap, tctx))
        summarize_targets = repaired_h
        logger.info("Completed pqa_summary (history ids): requested=%d, downloaded=%d, archived=%d", len(entry_ids), total_downloaded, len(downloaded_paths))
        # Fall through to optional summarization below

    # Skip topic-based DB scan when explicit --arxiv or --entry-id targets were provided;
    # in that mode the user wants only the specified papers, not the full ranked set.
    if not arxiv and not entry_ids:
        for t in topics:
            # If a specific topic is requested, only process that
            if topic and t != topic:
                continue

            # Load topic config and extract paperqa settings (cache for reuse in summarization)
            try:
                topic_cfg = cfg_mgr.load_topic_config(t)
                paperqa_cfg_topic = _get_topic_paperqa_config(topic_cfg, t)
                topic_cfg_cache[t] = topic_cfg
            except ValueError as e:
                logger.error(str(e))
                continue  # Skip topics without paperqa config
            except Exception as e:
                logger.error("Failed to load topic config for '%s': %s. Skipping.", t, e)
                continue

            # Extract topic-specific settings
            min_rank_topic = float(paperqa_cfg_topic.get('download_rank_threshold', 0.35))
            max_retries_topic = int(paperqa_cfg_topic.get('max_retries', DEFAULT_MAX_RETRIES))
            rps_topic = float(rps or paperqa_cfg_topic.get('rps', DEFAULT_RPS))
            min_interval_topic = max(3.0, 1.0 / max(rps_topic, 0.01))

            rows = _iter_ranked_entries(db, t, min_rank_topic)
            if limit is not None:
                rows = rows[: int(limit)]
            logger.info("Topic '%s': %d candidates with rank >= %.2f", t, len(rows), min_rank_topic)
            total_candidates += len(rows)

            for row in rows:
                arxiv_id = _resolve_arxiv_id(row)
                if not arxiv_id:
                    continue

                # Prefer versioned ID in file name if present
                fname_id = arxiv_id
                # Ensure filename-safe
                fname = f"{fname_id.replace('/', '_')}.pdf"
                dest_path = os.path.join(download_dir, fname)

                # Skip if already archived
                archived_path = _find_archived_pdf(archive_dir, arxiv_id)
                if archived_path:
                    logger.debug("Using archived PDF for %s: %s", arxiv_id, archived_path)
                    summarize_targets.append((row['id'], arxiv_id, archived_path, t))
                    continue
                # Skip if already downloaded in this folder
                if os.path.exists(dest_path):
                    logger.debug("Skipping %s (already downloaded)", arxiv_id)
                    downloaded_paths.append(dest_path)
                    summarize_targets.append((row['id'], arxiv_id, dest_path, t))
                    continue

                pdf_url = _query_arxiv_api_for_pdf(arxiv_id, mailto=mailto, session=sess) or f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=max_retries_topic)
                if ok:
                    downloaded_paths.append(dest_path)
                    summarize_targets.append((row['id'], arxiv_id, dest_path, t))
                    total_downloaded += 1
                    logger.info("Downloaded arXiv PDF: %s -> %s", arxiv_id, dest_path)
                else:
                    # Remove partial file if any
                    try:
                        if os.path.exists(dest_path):
                            os.remove(dest_path)
                    except OSError:
                        pass
                    logger.warning("Failed to download PDF for arXiv:%s", arxiv_id)

                # Polite delay (minimum 3 seconds per ToU; also covers PDF request)
                time.sleep(min_interval_topic)

    # Move all successfully downloaded PDFs to archive dir
    _move_to_archive(downloaded_paths, archive_dir)
    # Replace any targets that were in download_dir with archive paths
    repaired: List[Tuple[Optional[str], str, str, Optional[str]]] = []
    for eid, aid, path, tctx in summarize_targets:
        ap = _find_archived_pdf(archive_dir, aid) or path
        repaired.append((eid, aid, ap, tctx))
    summarize_targets = repaired
    logger.info("Completed PDF download: candidates=%d, downloaded=%d, archived=%d", total_candidates, total_downloaded, len(downloaded_paths))

    if not summarize:
        logger.info("Summarization not requested (use --summarize); %d PDF(s) ready", len(summarize_targets))
        _cleanup_archive(archive_dir)
        return

    if not summarize_targets:
        logger.info("No PDFs to summarize.")
        _cleanup_archive(archive_dir)
        return

    summarized = _summarize_targets(db, cfg_mgr, summarize_targets, topic_cfg_cache)
    logger.info("PDF summarization completed: wrote %d summaries", summarized)

    _cleanup_archive(archive_dir)

    # HTML generation is handled by the `html` command.
