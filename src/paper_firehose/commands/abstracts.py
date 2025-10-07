"""
Fetch abstracts and populate both papers.db (entries.abstract) and
matched_entries_history.db (matched_entries.abstract).

Rules:
- First pass fills arXiv/cond-mat abstracts from summary (no threshold).
- Then for rows with rank_score >= threshold: Crossref (DOI, then title search),
  followed by aggregator fallbacks (Semantic Scholar, OpenAlex, PubMed).
- Only process topics where topic yaml has abstract_fetch.enabled: true.
- Use per-topic abstract_fetch.rank_threshold if set; otherwise fall back to
  global defaults.rank_threshold in config.yaml.
- Respect API rate limits; include a descriptive User-Agent with contact email
  and obey Retry-After on 429/503. Default to ~1 request/second.
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

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
import logging


CROSSREF_API = "https://api.crossref.org/works/"


def _strip_jats(text: str | None) -> Optional[str]:
    """Remove JATS/HTML tags and unescape entities in Crossref-style strings."""
    if not text:
        return text
    # remove <jats:...> and regular HTML tags
    text = re.sub(r"</?jats:[^>]+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # unescape entities
    return htmllib.unescape(text).strip()


def _clean_for_db(text: Optional[str]) -> Optional[str]:
    """Conservative sanitizer for abstracts before storing in DB.

    - Removes JATS/HTML tags and unescapes entities via _strip_jats
    - Strips stray '<' and '>' characters (common artifact from feeds)
    - Removes leading feed prefixes like "Abstract" and arXiv announce headers
    - Normalizes whitespace and removes zero-width characters
    """
    if text is None:
        return None
    # First remove tags and unescape entities
    s = _strip_jats(text) or ""
    # Remove zero-width and BOM-like chars
    s = s.replace("\u200B", "").replace("\u200C", "").replace("\u200D", "").replace("\uFEFF", "")
    # Normalize non-breaking spaces
    s = s.replace("\xa0", " ")
    # Remove any remaining angle bracket characters which often leak from markup
    s = s.replace("<", "").replace(">", "")
    # Drop leading arXiv announce header like:
    #   "arXiv:2509.09390v1 Announce Type: new Abstract: ..."
    s = re.sub(r"^\s*arXiv:[^\n]*?(?:Announce\s+Type:\s*\w+\s+)?Abstract:\s*", "", s, flags=re.IGNORECASE)
    # Drop simple leading "Abstract" or "Abstract:" tokens
    s = re.sub(r"^\s*Abstract\s*:?[\s\-–—]*", "", s, flags=re.IGNORECASE)
    # Collapse excessive whitespace
    s = re.sub(r"[\t\r ]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _find_doi_in_text(text: Optional[str]) -> Optional[str]:
    """Return the first DOI-like token found in the provided string."""
    if not text:
        return None
    t = str(text).strip()
    if t.lower().startswith('doi:'):
        t = t[4:].strip()
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", t, flags=re.IGNORECASE)
    return m.group(0) if m else None


def _extract_doi_from_raw(raw: Optional[str]) -> Optional[str]:
    """Try to locate a DOI within the feed's raw JSON payload."""
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
    """Return the plain-text abstract for DOI or None if not available.

    Implements exponential backoff on 429/5xx and honors Retry-After when present.
    Also sends Crossref the mailto parameter.
    """
    sess = session or requests.Session()
    url = f"{CROSSREF_API}{quote(doi)}?mailto={quote(mailto)}"
    headers = {
        # Crossref asks for a descriptive UA with a contact email
        "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
    }
    for attempt in range(max_retries):
        try:
            r = sess.get(url, headers=headers, timeout=15)
            # Exponential backoff on throttling/server errors, prefer Retry-After
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except Exception:
                        wait = 1.0
                else:
                    wait = min(8.0, 2.0 ** attempt)
                time.sleep(wait if wait > 0 else 1.0)
                continue
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})
            abstract = msg.get("abstract")
            if abstract:
                return _strip_jats(abstract) or None
            return None
        except Exception:
            # Network or parsing error → backoff and retry
            time.sleep(min(8.0, 2.0 ** attempt))
            continue
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
    params = f"?query.bibliographic={quote(title)}&rows=1&mailto={quote(mailto)}"
    url = base + params
    headers = {
        "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
    }
    for attempt in range(max_retries):
        try:
            r = sess.get(url, headers=headers, timeout=15)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except Exception:
                        wait = 1.0
                else:
                    wait = min(8.0, 2.0 ** attempt)
                time.sleep(wait if wait > 0 else 1.0)
                continue
            r.raise_for_status()
            data = r.json()
            items = (data.get('message') or {}).get('items') or []
            if items:
                abstract = items[0].get('abstract')
                if abstract:
                    return _strip_jats(abstract) or None
            return None
        except Exception:
            time.sleep(min(8.0, 2.0 ** attempt))
            continue
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
    """Reassemble OpenAlex's inverted-index abstract representation."""
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
    """Fetch an abstract from OpenAlex by DOI, reconstructing when inverted-indexed."""
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
    """Look up a DOI in PubMed and return the combined abstract text if available."""
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
    """Yield ranked DB rows lacking abstracts for the given topic, highest score first."""
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


def _fill_arxiv_summaries(db: DatabaseManager, topics: Optional[list[str]] = None) -> int:
    """First pass: fill abstracts from summary for arXiv/cond-mat entries, no threshold.

    Returns number of rows updated.
    """
    import sqlite3
    conn = sqlite3.connect(db.db_paths['current'])
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
    updated = 0
    for id_, tpc, feed, link, summary in rows:
        if not summary:
            continue
        cleaned = _clean_for_db(summary)
        if cleaned:
            cur.execute("UPDATE entries SET abstract = ? WHERE id = ? AND topic = ?", (cleaned, id_, tpc))
            updated += 1
            # Also update history DB (best-effort)
            try:
                import sqlite3 as _sqlite3
                hconn = _sqlite3.connect(db.db_paths['history'])
                hcur = hconn.cursor()
                hcur.execute("UPDATE matched_entries SET abstract = ? WHERE entry_id = ?", (cleaned, id_))
                hconn.commit()
                hconn.close()
            except Exception:
                pass
    conn.commit()
    conn.close()
    return updated


def _crossref_pass(db: DatabaseManager, topic: str, threshold: float, *, mailto: str, session: requests.Session, min_interval: float, max_per_topic: Optional[int], max_retries: int = 3) -> int:
    """Second pass: Crossref only (DOI first, then title) for entries above threshold."""
    fetched = 0
    for row in _iter_targets(db, topic, threshold):
        doi = row.get('doi') or _extract_doi_from_raw(row.get('raw_data'))
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
            abstract = get_crossref_abstract(doi, mailto=mailto, session=session, max_retries=max_retries)
        time.sleep(min_interval)
        if not abstract:
            abstract = search_crossref_abstract_by_title(row.get('title') or '', mailto=mailto, session=session, max_retries=max_retries)
            time.sleep(min_interval)
        if abstract:
            abstract = _clean_for_db(abstract)
            import sqlite3
            conn = sqlite3.connect(db.db_paths['current'])
            cur = conn.cursor()
            cur.execute("UPDATE entries SET abstract = ? WHERE id = ? AND topic = ?", (abstract, row['id'], topic))
            conn.commit()
            conn.close()
            # Also update history DB (best-effort)
            try:
                import sqlite3 as _sqlite3
                hconn = _sqlite3.connect(db.db_paths['history'])
                hcur = hconn.cursor()
                hcur.execute("UPDATE matched_entries SET abstract = ? WHERE entry_id = ?", (abstract, row['id']))
                hconn.commit()
                hconn.close()
            except Exception:
                pass
            # Also update history DB (best-effort)
            try:
                import sqlite3 as _sqlite3
                hconn = _sqlite3.connect(db.db_paths['history'])
                hcur = hconn.cursor()
                hcur.execute("UPDATE matched_entries SET abstract = ? WHERE entry_id = ?", (abstract, row['id']))
                hconn.commit()
                hconn.close()
            except Exception:
                pass
            fetched += 1
            if max_per_topic is not None and fetched >= max_per_topic:
                break
    return fetched


def _fallback_pass(db: DatabaseManager, topic: str, threshold: float, *, mailto: str, session: requests.Session, min_interval: float, max_per_topic: Optional[int]) -> int:
    """Third pass: remaining above-threshold entries → Semantic Scholar / OpenAlex / PubMed."""
    fetched = 0
    for row in _iter_targets(db, topic, threshold):
        # Skip rows already filled by previous passes
        # _iter_targets already filters abstract IS NULL or empty
        doi = row.get('doi') or _extract_doi_from_raw(row.get('raw_data'))
        if not doi:
            try:
                raw = row.get('raw_data')
                if raw:
                    obj = json.loads(raw)
                    doi = obj.get('arxiv_doi') or obj.get('arXiv_doi')
            except Exception:
                pass
        abstract = try_publisher_apis(doi, row.get('feed_name') or '', row.get('link') or '', mailto=mailto, session=session)
        if abstract:
            abstract = _clean_for_db(abstract)
            import sqlite3
            conn = sqlite3.connect(db.db_paths['current'])
            cur = conn.cursor()
            cur.execute("UPDATE entries SET abstract = ? WHERE id = ? AND topic = ?", (abstract, row['id'], topic))
            conn.commit()
            conn.close()
            fetched += 1
            time.sleep(min_interval)
            if max_per_topic is not None and fetched >= max_per_topic:
                break
    return fetched


def run(config_path: str, topic: Optional[str] = None, *, mailto: Optional[str] = None, max_per_topic: Optional[int] = None, rps: float = 1.0) -> None:
    """Fetch and write abstracts into papers.db for ranked entries.

    Args:
        config_path: Path to the main configuration file (defaults to ~/.paper_firehose/config.yaml)
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
    abs_defaults = (defaults.get('abstracts') or {})

    # Resolve contact email: CLI arg -> MAILTO env -> config.defaults.abstracts.mailto -> fallback
    mailto = mailto or os.environ.get("MAILTO") or abs_defaults.get('mailto') or "nemesp@gmail.com"

    # RPS from config if provided
    if rps == 1.0:  # only use config if user didn't override
        try:
            rps_cfg = float(abs_defaults.get('rps')) if abs_defaults.get('rps') is not None else None
            if rps_cfg and rps_cfg > 0:
                rps = rps_cfg
        except Exception:
            pass
    max_retries = int(abs_defaults.get('max_retries', 3))

    sess = requests.Session()
    min_interval = 1.0 / max(rps, 0.01)

    # Step 1: First pass — fill arXiv/cond-mat abstracts from summaries (no threshold)
    filled = _fill_arxiv_summaries(db, topics)
    logger.info(f"Abstracts: arXiv/cond-mat summary fill updated={filled}")

    for t in topics:
        tcfg = cfg.load_topic_config(t)
        af_cfg = tcfg.get('abstract_fetch') or {}
        if not af_cfg or not af_cfg.get('enabled', False):
            logger.info(f"Abstract fetch disabled for topic '{t}', skipping")
            continue
        thr = float(af_cfg.get('rank_threshold', global_thresh))

        # Step 2: Crossref-only pass for above-threshold entries
        try:
            fetched_crossref = _crossref_pass(
                db, t, thr,
                mailto=mailto,
                session=sess,
                min_interval=min_interval,
                max_per_topic=max_per_topic,
                max_retries=max_retries,
            )
        except Exception as e:
            logger.error(f"Crossref pass failed for topic '{t}': {e}. Continuing with fallback providers.")
            fetched_crossref = 0
        # Step 3: Fallback APIs for remaining above-threshold entries
        try:
            fetched_fallback = _fallback_pass(db, t, thr, mailto=mailto, session=sess, min_interval=min_interval, max_per_topic=max_per_topic)
        except Exception as e:
            logger.error(f"Fallback providers pass failed for topic '{t}': {e}")
            fetched_fallback = 0
        logger.info(f"Abstracts: topic='{t}' threshold={thr} updated_crossref={fetched_crossref} updated_fallback={fetched_fallback}")

        # HTML generation is handled by the `html` command.

    # no return
