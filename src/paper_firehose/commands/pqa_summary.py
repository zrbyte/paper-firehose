"""
Paper-QA Summarizer

This command selects entries from papers.db for a given topic with
rank_score >= configured threshold and downloads arXiv PDFs for them,
adhering to arXiv API Terms of Use (polite rate limiting and descriptive
User-Agent with contact email).
- Run paper-qa over downloaded PDFs to produce grounded JSON summaries
- Write summaries into papers.db (entries.paper_qa_summary) and
  matched_entries_history.db (matched_entries.paper_qa_summary)
"""

from __future__ import annotations

import os
import re
import time
import shutil
import logging
import sqlite3
import asyncio
import threading
import inspect
from typing import Dict, Any, List, Optional, Tuple

import requests
import feedparser

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.paths import resolve_data_path

logger = logging.getLogger(__name__)


ARXIV_API = "https://export.arxiv.org/api/query"


def _ensure_dirs(download_dir: str, archive_dir: str) -> None:
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)


def _resolve_mailto(config: Dict[str, Any]) -> str:
    # Reuse abstracts mailto if available; otherwise fallback to env or placeholder
    defaults = (config.get('defaults') or {})
    abstracts = (defaults.get('abstracts') or {})
    mailto = abstracts.get('mailto') or os.environ.get('MAILTO') or 'you@example.org'
    return str(mailto)


def _arxiv_user_agent(mailto: str) -> str:
    # Include contact per arXiv API guidance
    return f"paper-firehose/pqa-summary (+mailto:{mailto})"


def _iter_ranked_entries(db: DatabaseManager, topic: str, min_rank: float) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db.db_paths['current'])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, topic, title, link, summary, doi, rank_score
        FROM entries
        WHERE topic = ? AND COALESCE(rank_score, 0) >= ?
        ORDER BY rank_score DESC
        """,
        (topic, float(min_rank)),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def _fetch_history_entries_by_ids(db: DatabaseManager, entry_ids: List[str], *, matched_date: Optional[str] = None, feed_like: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch rows from matched_entries_history.db for the given entry_ids.

    Optional filters:
    - matched_date: 'YYYY-MM-DD' exact match on the date portion
    - feed_like: substring to match in feed_name (case-insensitive)
    """
    if not entry_ids:
        return []
    import sqlite3
    conn = sqlite3.connect(db.db_paths['history'])
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
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    # Preserve input order
    idx = {eid: i for i, eid in enumerate(entry_ids)}
    rows.sort(key=lambda r: idx.get(r.get('entry_id', ''), 1e9))
    return rows


_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")


def _extract_arxiv_id_from_link(link: str | None) -> Optional[str]:
    if not link:
        return None
    try:
        if 'arxiv.org' not in link:
            return None
        # Common patterns: /abs/<id>(vN), /pdf/<id>(vN).pdf
        m = re.search(r"/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", link)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def _extract_arxiv_id_from_doi(doi: str | None) -> Optional[str]:
    if not doi:
        return None
    # Crossref canonical DOI for arXiv looks like: 10.48550/arXiv.2509.09390
    try:
        doi_l = doi.lower().strip()
        if doi_l.startswith("10.48550/arxiv."):
            return doi.split(".", 1)[1].replace("arXiv.", "") if "." in doi else doi.split("/", 1)[1].replace("arxiv.", "")
    except Exception:
        return None
    return None


def _extract_arxiv_id_from_text(text: str | None) -> Optional[str]:
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
    except Exception:
        return None
    return None


def _resolve_arxiv_id(entry: Dict[str, Any]) -> Optional[str]:
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
                except Exception:
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
                        except Exception:
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
        except Exception as e:
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
        except Exception as e:
            logger.warning("Failed to remove archived PDF %s: %s", path, e)
    if removed:
        logger.info("Removed %d archived PDFs older than %d days", removed, max_age_days)


def _call_paperqa_on_pdf(pdf_path: str, *, question: str) -> Optional[str]:
    """Summarize a PDF using paper-qa if available; return raw string answer."""
    try:
        from paperqa import Docs  # type: ignore
    except Exception as e:
        logger.error("paperqa not installed or import failed: %s", e)
        return None

    def _extract_answer(ans_obj: Any) -> Optional[str]:
        for attr in ("answer", "formatted_answer"):
            val = getattr(ans_obj, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        if isinstance(ans_obj, str) and ans_obj.strip():
            return ans_obj.strip()
        try:
            s = str(ans_obj).strip()
            return s if s else None
        except Exception:
            return None

    async def _run_async() -> Any:
        docs = Docs()
        # Prefer async methods when available; fall back to sync versions.
        add_fn = getattr(docs, "aadd", None)
        if callable(add_fn):
            await add_fn(pdf_path)
        else:
            res = docs.add(pdf_path)
            if inspect.isawaitable(res):
                await res

        query_fn = getattr(docs, "aquery", None)
        if callable(query_fn):
            return await query_fn(question)
        result = docs.query(question)
        if inspect.isawaitable(result):
            return await result
        return result

    try:
        try:
            ans_obj = asyncio.run(_run_async())
            return _extract_answer(ans_obj)
        except RuntimeError as exc:
            if "event loop" not in str(exc).lower():
                raise

        outcome: Dict[str, Any] = {}
        error: Dict[str, BaseException] = {}

        def _worker() -> None:
            new_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(new_loop)
                outcome['value'] = new_loop.run_until_complete(_run_async())
            except BaseException as exc:  # capture to re-raise outside thread
                error['error'] = exc
            finally:
                asyncio.set_event_loop(None)
                new_loop.close()

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join()

        if 'error' in error:
            raise error['error']

        return _extract_answer(outcome.get('value'))
    except Exception as e:
        logger.error("paperqa query failed for %s: %s", pdf_path, e)
        return None


def _normalize_summary_json(raw: str) -> Optional[str]:
    """Strip code fences, parse JSON, and ensure required keys exist.

    - Removes leading ```/```json and trailing ``` fences when present
    - Ensures keys: summary, methods (accepts aliases such as method/approach)
    - On parse failure, wraps cleaned text under 'summary' and sets 'methods' to ''
    """
    import json

    def _strip_fences(s: str) -> str:
        s2 = (s or '').strip()
        if s2.startswith('```'):
            # Drop first line (``` or ```json)
            nl = s2.find('\n')
            s2 = s2[nl + 1:] if nl != -1 else s2.lstrip('`')
        s2 = s2.rstrip()
        if s2.endswith('```'):
            s2 = s2[:-3].rstrip()
        return s2

    def _parse_obj(txt: str) -> Optional[dict]:
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _coerce_str(v) -> str:
        if v is None:
            return ''
        return v if isinstance(v, str) else str(v)

    cleaned = _strip_fences(raw)
    data = _parse_obj(cleaned)
    if data is None:
        # Try extract JSON between braces
        s = cleaned
        start = s.find('{')
        end = s.rfind('}')
        if start != -1 and end != -1 and end > start:
            data = _parse_obj(s[start:end+1])

    if data is None:
        out = {
            'summary': cleaned.strip(),
            'methods': '',
        }
        return json.dumps(out, ensure_ascii=False)

    def _first_nonempty(keys: Tuple[str, ...]) -> str:
        for key in keys:
            if key in data:
                val = _coerce_str(data.get(key))
                if val.strip():
                    return val
        return ''

    summary_val = _first_nonempty(('summary', 'overall_summary', 'answer', 'response', 'content'))
    if not summary_val.strip():
        summary_val = cleaned.strip()

    methods_val = _first_nonempty(('methods', 'method', 'approach', 'methodology', 'experimental_setup'))

    out = {
        'summary': summary_val,
        'methods': methods_val,
    }
    return json.dumps(out, ensure_ascii=False)


def _write_pqa_summary_to_dbs(db: DatabaseManager, entry_id: str, json_summary: str, *, topic: Optional[str] = None) -> None:
    """Write paper_qa_summary JSON into both current and history DBs.

    - papers.db: update the row for (id, topic) when topic is provided; otherwise update all rows with id = entry_id.
    - matched_entries_history.db: update row with entry_id
    """
    import sqlite3
    # Current DB
    try:
        conn = sqlite3.connect(db.db_paths['current'])
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
        conn.commit()
        conn.close()
        if topic:
            logger.info(
                "paper-qa DB write (papers.db): entry_id=%s topic=%s updated_rows=%d",
                entry_id,
                topic,
                updated_current,
            )
        else:
            logger.info(
                "paper-qa DB write (papers.db): entry_id=%s updated_rows=%d",
                entry_id,
                updated_current,
            )
    except Exception as e:
        logger.debug("Failed to write to papers.db for %s: %s", entry_id, e)
    # History DB
    try:
        hconn = sqlite3.connect(db.db_paths['history'])
        hcur = hconn.cursor()
        hcur.execute("UPDATE matched_entries SET paper_qa_summary = ? WHERE entry_id = ?", (json_summary, entry_id))
        updated_history = hcur.rowcount
        hconn.commit()
        hconn.close()
        logger.info("paper-qa DB write (history.db): entry_id=%s updated_rows=%d", entry_id, updated_history)
    except Exception as e:
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
) -> None:
    """Execute the paper-qa download + summarization workflow.

    Workflow overview:
    - Load configuration/database state and prepare download/archive folders.
    - Determine targets either from ranked topic entries (respecting the download
      rank threshold and optional ``limit``) or from the explicit ``arxiv``/``entry_ids``
      arguments, optionally pulling metadata from the history database when
      ``use_history`` is enabled.
    - Resolve arXiv IDs, reuse archived PDFs when possible, download missing
      PDFs under the configured rate limit, and archive successful downloads.
    - Run paper-qa on each PDF, normalize the JSON result, and write summaries
      back to both ``papers.db`` and ``matched_entries_history.db`` when an
      ``entry_id`` is available.
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

    paperqa_cfg = (config.get('paperqa') or {})
    min_rank = float(paperqa_cfg.get('download_rank_threshold', 0.35))
    max_retries = int(paperqa_cfg.get('max_retries', 3))
    # arXiv API guidance suggests ~1 request/3 seconds; use the stricter of config and this default
    rps_cfg = float(paperqa_cfg.get('rps', 0.3))
    rps_eff = rps if rps is not None else rps_cfg
    min_interval = max(3.0, 1.0 / max(rps_eff, 0.01))

    _ensure_dirs(download_dir, archive_dir)

    topics: List[str] = [topic] if topic else cfg_mgr.get_available_topics()
    mailto = _resolve_mailto(config)

    downloaded_paths: List[str] = []
    summarize_targets: List[Tuple[Optional[str], str, str, Optional[str]]] = []  # (entry_id, arxiv_id, pdf_path, topic_ctx)
    sess = requests.Session()

    total_candidates = 0
    total_downloaded = 0

    # Manual mode: download specific arXiv IDs/URLs if provided
    if arxiv:
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
                summarize_targets.append((None, arxiv_id, archived_path, None))
                continue
            if os.path.exists(dest_path):
                logger.debug("Skipping %s (already downloaded)", arxiv_id)
                downloaded_paths.append(dest_path)
                summarize_targets.append((None, arxiv_id, dest_path, None))
                continue
            pdf_url = _query_arxiv_api_for_pdf(arxiv_id, mailto=mailto, session=sess) or f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=max_retries)
            if ok:
                downloaded_paths.append(dest_path)
                summarize_targets.append((None, arxiv_id, dest_path, None))
                total_downloaded += 1
                logger.info("Downloaded arXiv PDF: %s -> %s", arxiv_id, dest_path)
            else:
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception:
                    pass
                logger.warning("Failed to download PDF for arXiv:%s", arxiv_id)
            time.sleep(min_interval)

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
            ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=max_retries)
            if ok:
                downloaded_paths.append(dest_path)
                summarize_targets.append((row.get('entry_id'), arxiv_id, dest_path, topic_ctx))
                total_downloaded += 1
                logger.info("Downloaded arXiv PDF: %s -> %s (entry_id=%s)", arxiv_id, dest_path, row.get('entry_id'))
            else:
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception:
                    pass
                logger.warning("Failed to download PDF for arXiv:%s (entry_id=%s)", arxiv_id, row.get('entry_id'))
            time.sleep(min_interval)

        _move_to_archive(downloaded_paths, archive_dir)
        # Repair target paths to point at archive if needed
        repaired_h: List[Tuple[Optional[str], str, str, Optional[str]]] = []
        for eid, aid, path, tctx in summarize_targets:
            ap = _find_archived_pdf(archive_dir, aid) or path
            repaired_h.append((eid, aid, ap, tctx))
        summarize_targets = repaired_h
        logger.info("Completed pqa_summary (history ids): requested=%d, downloaded=%d, archived=%d", len(entry_ids), total_downloaded, len(downloaded_paths))
        # Fall through to optional summarization below

    for t in topics:
        # If a specific topic is requested, only process that
        if topic and t != topic:
            continue

        rows = _iter_ranked_entries(db, t, min_rank)
        if limit is not None:
            rows = rows[: int(limit)]
        logger.info("Topic '%s': %d candidates with rank >= %.2f", t, len(rows), min_rank)
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
            ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=max_retries)
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
                except Exception:
                    pass
                logger.warning("Failed to download PDF for arXiv:%s", arxiv_id)

            # Polite delay (minimum 3 seconds per ToU; also covers PDF request)
            time.sleep(min_interval)

    # Move all successfully downloaded PDFs to archive dir
    _move_to_archive(downloaded_paths, archive_dir)
    # Replace any targets that were in download_dir with archive paths
    repaired: List[Tuple[Optional[str], str, str, Optional[str]]] = []
    for eid, aid, path, tctx in summarize_targets:
        ap = _find_archived_pdf(archive_dir, aid) or path
        repaired.append((eid, aid, ap, tctx))
    summarize_targets = repaired
    logger.info("Completed pqa_summary: candidates=%d, downloaded=%d, archived=%d", total_candidates, total_downloaded, len(downloaded_paths))

    if not summarize_targets:
        logger.info("No PDFs to summarize.")
        _cleanup_archive(archive_dir)
        return

    summarized = 0
    for eid, aid, pdf_path, tctx in summarize_targets:
        # Build paper-qa question from config (per-item to allow topic-aware placeholder substitution)
        question = (paperqa_cfg.get('prompt') or '').strip()
        if not question:
            question = (
                "You are an expert technical reader. Summarize this paper for experts. "
                "Return ONLY a JSON object with keys: 'summary', 'methods'. "
                "Keep each value concise and information dense."
            )
        if tctx:
            try:
                tcfg = cfg_mgr.load_topic_config(tctx)
                rq = ((tcfg.get('ranking') or {}).get('query') or '').strip()
                if rq and '{ranking_query}' in question:
                    question = question.replace('{ranking_query}', rq)
            except Exception:
                pass
        # For manual arxiv mode, eid may be None; skip DB write if missing
        raw_ans = _call_paperqa_on_pdf(pdf_path, question=question)
        if not raw_ans:
            continue
        # Output the raw paper-qa response for inspection
        try:
            logger.info("paper-qa raw response (entry_id=%s, arXiv=%s):\n%s", eid or "-", aid, raw_ans)
        except Exception:
            # Best-effort logging; ignore formatting failures
            pass
        # Normalize and write
        norm = _normalize_summary_json(raw_ans)
        if not norm:
            # As a last resort, write the raw response
            norm = raw_ans
        if eid:
            _write_pqa_summary_to_dbs(db, eid, norm, topic=tctx)
            summarized += 1
        else:
            # No entry id: history-only test not possible; skip write
            logger.debug("Got summary for %s but no entry_id present; skipping DB write", aid)

    logger.info("paper-qa summarization completed: wrote %d summaries", summarized)

    _cleanup_archive(archive_dir)

    # HTML generation is handled by the `html` command.
