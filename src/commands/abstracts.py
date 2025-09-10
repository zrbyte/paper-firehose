"""
Fetch abstracts from Crossref for high-ranked entries in papers.db.

Rules:
- Only process topics where topic yaml has abstract_fetch.enabled: true
- Use per-topic abstract_fetch.rank_threshold if set; otherwise fall back to
  global defaults.rank_threshold in config.yaml.
- Only fetch for rows with rank_score >= threshold and empty abstract.
- Respect Crossref rate limits: include a descriptive User-Agent with contact
  email and obey Retry-After on 429/503. Default to ~1 request/second.
"""

from __future__ import annotations

import time
import json
import html as htmllib
import re
from urllib.parse import quote
from typing import Optional, Dict, Any, Iterable
import os

import requests

from core.config import ConfigManager
from core.database import DatabaseManager
import logging


CROSSREF_API = "https://api.crossref.org/works/"


def _strip_jats(text: str | None) -> Optional[str]:
    if not text:
        return text
    # remove <jats:...> and regular HTML tags
    text = re.sub(r"</?jats:[^>]+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # unescape entities
    return htmllib.unescape(text).strip()


def _find_doi_in_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = str(text).strip()
    if t.lower().startswith('doi:'):
        t = t[4:].strip()
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", t, flags=re.IGNORECASE)
    return m.group(0) if m else None


def _extract_doi_from_raw(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    # Try common fields and fallbacks
    for key in [
        'doi', 'dc_identifier', 'dc:identifier', 'dc.identifier', 'prism:doi',
        'id', 'link', 'summary', 'description'
    ]:
        v = obj.get(key)
        doi = _find_doi_in_text(v)
        if doi:
            return doi
    # Check nested content and links arrays
    contents = obj.get('content') or []
    if isinstance(contents, list):
        for c in contents:
            if isinstance(c, dict):
                doi = _find_doi_in_text(c.get('value') or c.get('content'))
                if doi:
                    return doi
    links = obj.get('links') or []
    if isinstance(links, list):
        for l in links:
            href = l.get('href') if isinstance(l, dict) else str(l)
            doi = _find_doi_in_text(href)
            if doi:
                return doi
    return None


def get_crossref_abstract(doi: str, *, mailto: str, max_retries: int = 3, session: Optional[requests.Session] = None) -> Optional[str]:
    """Return the plain-text abstract for DOI or None if not available."""
    sess = session or requests.Session()
    url = CROSSREF_API + quote(doi)
    headers = {
        # Crossref asks for a descriptive UA with a contact email
        "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
    }
    for attempt in range(max_retries):
        r = sess.get(url, headers=headers, timeout=15)
        # Respect Retry-After on 429/503
        if r.status_code in (429, 503):
            wait = int(r.headers.get("Retry-After", "1"))
            time.sleep(wait if wait > 0 else 1)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {})
        abstract = msg.get("abstract")
        if abstract:
            return _strip_jats(abstract) or None
        return None
    return None


def search_crossref_abstract_by_title(title: str, *, mailto: str, max_retries: int = 2, session: Optional[requests.Session] = None) -> Optional[str]:
    """Best-effort abstract lookup by title when DOI is missing or returns no abstract.

    Uses Crossref's works search endpoint with a bibliographic query. Returns the
    first item's abstract if available.
    """
    if not title:
        return None
    sess = session or requests.Session()
    base = "https://api.crossref.org/works"
    params = f"?query.bibliographic={quote(title)}&rows=1"
    url = base + params
    headers = {
        "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
    }
    for attempt in range(max_retries):
        r = sess.get(url, headers=headers, timeout=15)
        if r.status_code in (429, 503):
            wait = int(r.headers.get("Retry-After", "1"))
            time.sleep(wait if wait > 0 else 1)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        items = (data.get('message') or {}).get('items') or []
        if items:
            abstract = items[0].get('abstract')
            if abstract:
                return _strip_jats(abstract) or None
        return None
    return None


def get_semantic_scholar_abstract(doi: str, *, session: Optional[requests.Session] = None) -> Optional[str]:
    """Fetch abstract from Semantic Scholar Graph API by DOI (no key needed)."""
    if not doi:
        return None
    sess = session or requests.Session()
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi)}?fields=abstract"
    try:
        r = sess.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        abs_txt = data.get('abstract')
        return _strip_jats(abs_txt) if abs_txt else None
    except Exception:
        return None


def _reconstruct_openalex(ii: Dict[str, Any]) -> Optional[str]:
    try:
        idx_pairs = []
        max_pos = -1
        for word, positions in ii.items():
            for p in positions:
                if p > max_pos:
                    max_pos = p
                idx_pairs.append((p, word))
        if max_pos < 0:
            return None
        arr = [None] * (max_pos + 1)
        for pos, word in idx_pairs:
            arr[pos] = word
        return ' '.join(w for w in arr if w)
    except Exception:
        return None


def get_openalex_abstract(doi: str, *, mailto: str, session: Optional[requests.Session] = None) -> Optional[str]:
    if not doi:
        return None
    sess = session or requests.Session()
    url = f"https://api.openalex.org/works/https://doi.org/{quote(doi)}?mailto={quote(mailto)}"
    try:
        r = sess.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        abs_txt = data.get('abstract')
        if abs_txt:
            return _strip_jats(abs_txt)
        ii = data.get('abstract_inverted_index')
        if ii:
            return _reconstruct_openalex(ii)
        return None
    except Exception:
        return None


def get_pubmed_abstract_by_doi(doi: str, *, session: Optional[requests.Session] = None) -> Optional[str]:
    if not doi:
        return None
    sess = session or requests.Session()
    try:
        # ESearch for PMID by DOI
        es = sess.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"},
            timeout=15,
        )
        es.raise_for_status()
        idlist = (es.json().get('esearchresult') or {}).get('idlist') or []
        if not idlist:
            return None
        pmid = idlist[0]
        # EFetch to get abstract XML
        ef = sess.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pubmed", "id": pmid, "retmode": "xml"},
            timeout=15,
        )
        ef.raise_for_status()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(ef.text)
        texts = []
        for at in root.findall('.//AbstractText'):
            texts.append(''.join(at.itertext()).strip())
        return _strip_jats(' '.join(t for t in texts if t)) if texts else None
    except Exception:
        return None


def try_publisher_apis(doi: Optional[str], feed_name: str, link: str, *, mailto: str, session: Optional[requests.Session]) -> Optional[str]:
    """Try publisher/aggregator APIs based on journal or domain.

    Order (by common coverage): Semantic Scholar, OpenAlex; for PNAS (or biomedical), try PubMed.
    """
    fn = (feed_name or '').lower()
    domain = (link or '').lower()

    # PNAS or biomedical journals: try PubMed first
    if 'pnas' in fn or 'pnas.org' in domain:
        abs_txt = get_pubmed_abstract_by_doi(doi or '', session=session)
        if abs_txt:
            return abs_txt
    # Generic: Semantic Scholar then OpenAlex
    abs_txt = get_semantic_scholar_abstract(doi or '', session=session)
    if abs_txt:
        return abs_txt
    abs_txt = get_openalex_abstract(doi or '', mailto=mailto, session=session)
    if abs_txt:
        return abs_txt
    # Final PubMed attempt even if not PNAS (some Nature/Science items are indexed)
    abs_txt = get_pubmed_abstract_by_doi(doi or '', session=session)
    return abs_txt


def _iter_targets(db: DatabaseManager, topic: str, threshold: float) -> Iterable[Dict[str, Any]]:
    # Query directly for performance
    import sqlite3
    conn = sqlite3.connect(db.db_paths['current'])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, topic, doi, abstract, rank_score, raw_data, title, feed_name, summary, link
        FROM entries
        WHERE topic = ?
          AND (abstract IS NULL OR TRIM(abstract) = '')
          AND rank_score IS NOT NULL
          AND rank_score >= ?
        ORDER BY rank_score DESC
        """,
        (topic, threshold),
    )
    cols = [d[0] for d in cur.description]
    for row in cur.fetchall():
        yield dict(zip(cols, row))
    conn.close()


def run(config_path: str, topic: Optional[str] = None, *, mailto: Optional[str] = None, max_per_topic: Optional[int] = None, rps: float = 1.0) -> None:
    """Fetch and write abstracts into papers.db for ranked entries.

    Args:
        config_path: Path to config/config.yaml
        topic: Optional single topic; otherwise process all topics
        mailto: Contact email for Crossref User-Agent
        max_per_topic: Optional cap on number of fetches per topic
        rps: Requests per second throttle (default ~1 req/s)
    """
    logger = logging.getLogger(__name__)
    cfg = ConfigManager(config_path)
    config = cfg.load_config()
    db = DatabaseManager(config)

    topics = [topic] if topic else cfg.get_available_topics()
    # Default threshold
    defaults = (config.get('defaults') or {})
    global_thresh = float(defaults.get('rank_threshold', 0.35))

    # Resolve contact email: CLI arg -> MAILTO env -> default
    mailto = mailto or os.environ.get("MAILTO", "nemesp@gmail.com")

    sess = requests.Session()
    min_interval = 1.0 / max(rps, 0.01)

    for t in topics:
        tcfg = cfg.load_topic_config(t)
        af_cfg = tcfg.get('abstract_fetch') or {}
        if not af_cfg or not af_cfg.get('enabled', False):
            logger.info(f"Abstract fetch disabled for topic '{t}', skipping")
            continue
        thr = float(af_cfg.get('rank_threshold', global_thresh))

        total = 0
        with_doi = 0
        fetched = 0
        for row in _iter_targets(db, t, thr):
            total += 1
            # If this is an arXiv cond-mat entry, use the summary as abstract
            feed_name = (row.get('feed_name') or '').lower()
            link = (row.get('link') or '').lower()
            if 'cond-mat' in feed_name or 'arxiv' in feed_name or 'arxiv.org' in link:
                summary = row.get('summary') or ''
                abstract_from_summary = _strip_jats(summary) if summary else ''
                if abstract_from_summary:
                    import sqlite3
                    conn = sqlite3.connect(db.db_paths['current'])
                    cur = conn.cursor()
                    cur.execute("UPDATE entries SET abstract = ? WHERE id = ? AND topic = ?", (abstract_from_summary, row['id'], t))
                    conn.commit()
                    conn.close()
                    fetched += 1
                    # Skip network sources for this row
                    if max_per_topic is not None and fetched >= max_per_topic:
                        break
                    else:
                        continue
            doi = row.get('doi') or _extract_doi_from_raw(row.get('raw_data'))
            # Also check arXiv-specific DOI field in raw JSON
            if not doi:
                try:
                    raw = row.get('raw_data')
                    if raw:
                        obj = json.loads(raw)
                        doi = obj.get('arxiv_doi') or obj.get('arXiv_doi')
                except Exception:
                    pass
            abstract: Optional[str] = None
            if doi:
                with_doi += 1
                abstract = get_crossref_abstract(doi, mailto=mailto, session=sess)
            # Throttle between requests, even on None, to be polite
            time.sleep(min_interval)
            # Fallback: try title search if no DOI abstract and DOI missing or returned None
            if not abstract:
                abstract = search_crossref_abstract_by_title(row.get('title') or '', mailto=mailto, session=sess)
                time.sleep(min_interval)
            # Try publisher/aggregator APIs by journal/domain (no scraping)
            if not abstract:
                abstract = try_publisher_apis(doi, row.get('feed_name') or '', row.get('link') or '', mailto=mailto, session=sess)
                if abstract:
                    time.sleep(min_interval)
            if abstract:
                # Write into DB
                import sqlite3
                conn = sqlite3.connect(db.db_paths['current'])
                cur = conn.cursor()
                cur.execute("UPDATE entries SET abstract = ? WHERE id = ? AND topic = ?", (abstract, row['id'], t))
                conn.commit()
                conn.close()
                fetched += 1
            if max_per_topic is not None and fetched >= max_per_topic:
                break
        logger.info(f"Abstracts: topic='{t}' threshold={thr} candidates={total} with_doi={with_doi} updated={fetched}")

    # no return
