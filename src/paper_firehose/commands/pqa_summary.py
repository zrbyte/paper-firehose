"""
Paper-QA Summarizer
===================

This command selects entries from papers.db for a given topic with
``rank_score >=`` the configured threshold and downloads arXiv PDFs for them,
adhering to arXiv API Terms of Use (polite rate limiting and descriptive
``User-Agent`` with contact email).

Workflow
--------

1. Load configuration and identify ranked entries above threshold
2. Download arXiv PDFs (respecting rate limits, reusing archived copies)
3. Run ``paper-qa`` over each PDF to produce grounded JSON summaries
4. Write summaries into papers.db and matched_entries_history.db

Architecture: PaperQASession
----------------------------

The paper-qa library uses a persistent index stored in ``~/.pqa/`` (or wherever
``PQA_HOME`` points). This creates a critical issue when processing multiple PDFs:

**The Problem:**

Paper-qa reads and caches ``PQA_HOME`` at *import time*. Python's import system
caches modules, so subsequent ``import paperqa`` statements return the cached
module without re-reading environment variables. This means:

1. First PDF: Set PQA_HOME=/tmp/A, import paperqa → paperqa caches /tmp/A
2. Process PDF successfully, clean up /tmp/A
3. Second PDF: Set PQA_HOME=/tmp/B, import paperqa → NO-OP, module cached!
4. Paperqa still uses /tmp/A (now deleted) → "no papers found" error

**The Solution: PaperQASession**

We use a context manager that:

1. Creates ONE temp directory for the entire summarization session
2. Sets ``PQA_HOME`` and changes working directory BEFORE importing paperqa
3. Imports paperqa ONCE (it correctly caches the session directory)
4. Processes each PDF in isolated paper/index directories
5. Cleans up the session directory on exit

This ensures paperqa always sees a valid, consistent environment throughout
the entire run, regardless of how many PDFs are processed.

**Why it works locally but fails on VPS:**

- Local: Fresh Python process per test → import cache empty → works
- VPS (pipx): Long-running process or module caching → import cache populated
  from first PDF → subsequent PDFs fail

Usage Example
-------------

.. code-block:: python

    with PaperQASession(llm='gpt-4o', summary_llm='gpt-4o-mini') as session:
        for pdf_path in pdf_paths:
            answer = session.summarize_pdf(pdf_path, "Summarize this paper")
            if answer:
                print(answer)
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
import warnings
from typing import Dict, Any, List, Optional, Tuple

import requests
import feedparser

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.command_utils import resolve_topics
from ..core.paths import resolve_data_path

logger = logging.getLogger(__name__)

# Suppress noisy async/litellm warnings that clutter output
logging.getLogger("litellm").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=".*bound to a different event loop.*")
warnings.filterwarnings("ignore", message=".*coroutine.*was never awaited.*")


ARXIV_API = "https://export.arxiv.org/api/query"


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
    except Exception:
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
    except Exception:
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
    except Exception:
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


class PaperQASession:
    """Context manager for processing multiple PDFs with paper-qa.

    This class solves a critical issue with paper-qa's environment handling:
    paper-qa reads ``PQA_HOME`` at import time and caches it internally.
    Python's import system caches modules, so setting ``PQA_HOME`` before
    subsequent imports has no effect - the module is already loaded with
    the old value.

    Solution Architecture
    ---------------------

    Instead of creating a new temp directory for each PDF (which fails after
    the first PDF because paperqa is already imported with the old path),
    we create ONE session directory and process all PDFs within it:

    .. code-block:: text

        Session Start
        ├── Create /tmp/paperqa_session_xxx/
        ├── Set PQA_HOME=/tmp/paperqa_session_xxx
        ├── Change CWD to /tmp/paperqa_session_xxx
        └── Import paperqa (caches /tmp/paperqa_session_xxx) ✓

        For each PDF:
        ├── Create /tmp/paperqa_session_xxx/paper_*/ and index_*/ dirs
        ├── Copy PDF into paper_*/ and run ask() with those dirs
        └── Remove per-PDF dirs to keep runs isolated

        Session End
        ├── Restore original CWD
        ├── Restore original PQA_HOME
        └── Delete /tmp/paperqa_session_xxx/

    Key Design Decisions
    --------------------

    1. **Single import**: paperqa is imported exactly once per session, so
       it correctly caches the session's temp directory.

    2. **Per-PDF isolation**: Each PDF gets its own paper/index directories,
       so there is no cross-contamination or stale index state between runs.

    3. **PDF removal**: We remove each PDF after processing to ensure
       paper-qa only sees one PDF at a time during indexing.

    4. **Environment restoration**: We carefully restore ``PQA_HOME`` and
       CWD on exit, even if an exception occurs.

    Attributes
    ----------
    llm : str or None
        The LLM model to use for paper-qa (e.g., 'gpt-4o', 'gpt-5.2').
    summary_llm : str or None
        The summary LLM model (e.g., 'gpt-4o-mini').
    temp_dir : str or None
        Path to the session's temporary directory (set on __enter__).
    original_cwd : str or None
        The working directory before session start (for restoration).
    original_pqa_home : str or None
        The PQA_HOME value before session start (for restoration).

    Example
    -------
    .. code-block:: python

        pdf_paths = ['/path/to/paper1.pdf', '/path/to/paper2.pdf']

        with PaperQASession(llm='gpt-4o', summary_llm='gpt-4o-mini') as session:
            for pdf in pdf_paths:
                answer = session.summarize_pdf(pdf, "Summarize this paper")
                if answer:
                    process_answer(answer)
        # Environment automatically restored, temp files cleaned up
    """

    def __init__(self, llm: Optional[str] = None, summary_llm: Optional[str] = None):
        """Initialize session configuration (does not set up environment yet).

        The actual environment setup happens in __enter__ to support the
        context manager pattern. This allows proper cleanup even if
        setup fails partway through.

        Parameters
        ----------
        llm : str, optional
            LLM model for paper-qa's main reasoning (e.g., 'gpt-4o').
            If None, uses paper-qa's default.
        summary_llm : str, optional
            LLM model for paper-qa's summarization steps (e.g., 'gpt-4o-mini').
            If None, uses paper-qa's default.
        """
        self.llm = llm
        self.summary_llm = summary_llm

        # Session state (populated in __enter__)
        self.temp_dir: Optional[str] = None
        self.original_cwd: Optional[str] = None
        self.original_pqa_home: Optional[str] = None

        # Paper-qa references (populated after import in __enter__)
        self._settings_class: Optional[type] = None
        self._ask_func: Optional[Any] = None
        self._initialized = False

    def __enter__(self) -> 'PaperQASession':
        """Set up isolated environment and import paper-qa.

        This method performs the critical setup sequence:

        1. Create temporary directory structure
        2. Save current environment state for later restoration
        3. Set PQA_HOME to temp directory (BEFORE import!)
        4. Change working directory to temp (BEFORE import!)
        5. Import paper-qa (it caches our temp directory)
        6. Configure litellm if using GPT-5 models

        The order is critical: PQA_HOME and CWD must be set BEFORE
        importing paperqa, because paperqa reads these at import time
        and caches them.

        Returns
        -------
        PaperQASession
            Self, for use in 'with' statement.

        Raises
        ------
        ImportError
            If paper-qa is not installed or fails to import.
        """
        import tempfile
        import pathlib

        # ============================================================
        # STEP 1: Create session directory structure
        # ============================================================
        # We use a unique temp directory per session. All PDFs will be
        # copied here, processed, then removed.
        self.temp_dir = tempfile.mkdtemp(prefix='paperqa_session_')

        # ============================================================
        # STEP 2: Save original environment for restoration in __exit__
        # ============================================================
        self.original_cwd = os.getcwd()
        self.original_pqa_home = os.environ.get('PQA_HOME')

        # ============================================================
        # STEP 3: Set PQA_HOME BEFORE importing paperqa
        # ============================================================
        # CRITICAL: This must happen BEFORE the import statement below.
        # Paper-qa reads PQA_HOME at import time and uses it for:
        # - Index storage location (~/.pqa/indexes/ by default)
        # - Settings defaults
        # - Other internal paths
        os.environ['PQA_HOME'] = self.temp_dir
        logger.debug(f"PaperQASession: Set PQA_HOME to {self.temp_dir}")

        # ============================================================
        # STEP 4: Change CWD BEFORE importing paperqa
        # ============================================================
        # Paper-qa may also use CWD for file discovery. By changing to
        # the temp directory, we ensure it won't find any files from
        # the user's home directory (like .claude/plugins/ on VPS).
        os.chdir(self.temp_dir)
        logger.debug(f"PaperQASession: Changed CWD to {self.temp_dir}")

        # ============================================================
        # STEP 5: Import paper-qa (it will cache our temp directory)
        # ============================================================
        # This is the key moment: paperqa reads PQA_HOME and CWD NOW,
        # and caches them for the lifetime of the Python process.
        # Since we set them to our temp directory above, paperqa will
        # use our temp directory for everything.
        try:
            from paperqa import Settings, ask
            self._settings_class = Settings
            self._ask_func = ask
            self._initialized = True
            logger.debug("PaperQASession: Imported paperqa successfully")
        except Exception as e:
            logger.error(f"PaperQASession: Failed to import paperqa: {e}")
            # Clean up on failure
            self.__exit__(None, None, None)
            raise

        # ============================================================
        # STEP 6: Configure litellm for GPT-5 compatibility (optional)
        # ============================================================
        # GPT-5 models have different parameter requirements. Setting
        # drop_params=True tells litellm to silently drop unsupported
        # parameters instead of raising errors.
        try:
            import litellm
            is_gpt5 = (self.llm and 'gpt-5' in self.llm.lower()) or \
                      (self.summary_llm and 'gpt-5' in self.summary_llm.lower())
            if is_gpt5:
                litellm.drop_params = True
                logger.info("Enabled litellm.drop_params for GPT-5 model compatibility")
        except Exception as e:
            logger.debug(f"Could not configure litellm: {e}")

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Restore original environment and clean up temp directory.

        This method is called automatically when exiting the 'with' block,
        even if an exception occurred. It restores the original working
        directory and PQA_HOME, then deletes the temporary directory.

        Parameters
        ----------
        exc_type : type or None
            Exception type if an exception was raised, None otherwise.
        exc_val : BaseException or None
            Exception instance if an exception was raised, None otherwise.
        exc_tb : traceback or None
            Traceback if an exception was raised, None otherwise.

        Note
        ----
        We don't re-raise exceptions here (return value is implicitly None),
        so any exceptions from the 'with' block propagate normally.
        """
        # ============================================================
        # Restore original working directory
        # ============================================================
        if self.original_cwd:
            try:
                os.chdir(self.original_cwd)
                logger.debug(f"PaperQASession: Restored CWD to {self.original_cwd}")
            except Exception as e:
                logger.warning(f"Failed to restore working directory: {e}")

        # ============================================================
        # Restore original PQA_HOME environment variable
        # ============================================================
        # If PQA_HOME was set before our session, restore it.
        # If it wasn't set, remove it from the environment.
        try:
            if self.original_pqa_home is not None:
                os.environ['PQA_HOME'] = self.original_pqa_home
            elif 'PQA_HOME' in os.environ:
                del os.environ['PQA_HOME']
            logger.debug("PaperQASession: Restored PQA_HOME")
        except Exception as e:
            logger.warning(f"Failed to restore PQA_HOME: {e}")

        # ============================================================
        # Delete the temporary session directory
        # ============================================================
        # This removes all PDFs, index files, and any other temporary
        # files created during the session.
        if self.temp_dir:
            try:
                shutil.rmtree(self.temp_dir)
                logger.debug(f"PaperQASession: Cleaned up {self.temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory: {e}")

    def summarize_pdf(self, pdf_path: str, question: str) -> Optional[str]:
        """Process a single PDF and return paper-qa's answer as a string.

        This method handles the per-PDF processing workflow:

        1. Create per-PDF paper and index directories
        2. Copy the PDF into the per-PDF paper directory
        3. Build paper-qa Settings with those isolated directories
        3. Run paper-qa's ask() function asynchronously
        4. Extract the answer string from paper-qa's response object
        5. Clean up: remove per-PDF directories to keep runs isolated

        Each run uses fresh directories, so paper-qa only sees one PDF
        per call without having to clear or reconcile a shared index.

        Parameters
        ----------
        pdf_path : str
            Absolute path to the PDF file to process. The file is copied
            to the session's temp directory, so the original is not modified.
        question : str
            The question to ask paper-qa about the PDF. This is typically
            a prompt asking for a JSON-formatted summary with 'summary'
            and 'methods' keys.

        Returns
        -------
        str or None
            The answer string from paper-qa if successful, None if:
            - Session not initialized (called outside 'with' block)
            - PDF copy failed
            - paper-qa query failed
            - Answer extraction failed

        Note
        ----
        This method handles async/event loop edge cases:
        - If asyncio.run() fails due to existing event loop, we spawn
          a background thread with its own event loop
        - This is necessary because Jupyter notebooks and some frameworks
          already have an event loop running

        Example
        -------
        .. code-block:: python

            with PaperQASession() as session:
                answer = session.summarize_pdf(
                    '/path/to/paper.pdf',
                    'Summarize this paper. Return JSON with summary and methods.'
                )
                if answer:
                    data = json.loads(answer)
        """
        import pathlib

        # ============================================================
        # Validate session state
        # ============================================================
        if not self._initialized:
            logger.error("PaperQASession not initialized")
            return None

        # ============================================================
        # STEP 1: Create per-PDF paper/index directories
        # ============================================================
        # Use per-PDF dirs to avoid stale index entries or cross-contamination.
        import tempfile

        paper_dir = tempfile.mkdtemp(prefix='paper_', dir=self.temp_dir)
        index_dir = tempfile.mkdtemp(prefix='index_', dir=self.temp_dir)
        pdf_name = os.path.basename(pdf_path)
        temp_pdf_path = os.path.join(paper_dir, pdf_name)

        try:
            shutil.copy2(pdf_path, temp_pdf_path)
            logger.debug(f"Copied PDF to session: {temp_pdf_path}")
        except Exception as e:
            logger.error(f"Failed to copy PDF {pdf_path}: {e}")
            try:
                if os.path.exists(paper_dir):
                    shutil.rmtree(paper_dir)
                if os.path.exists(index_dir):
                    shutil.rmtree(index_dir)
            except Exception:
                pass
            return None

        try:
            # ============================================================
            # STEP 2: Build paper-qa Settings
            # ============================================================
            # We explicitly set paper_directory and index_directory to
            # ensure paper-qa uses our per-PDF directories, not defaults.
            settings_kwargs: Dict[str, Any] = {
                'paper_directory': pathlib.Path(paper_dir),
                'index_directory': pathlib.Path(index_dir),
            }
            if self.llm:
                settings_kwargs['llm'] = self.llm
            if self.summary_llm:
                settings_kwargs['summary_llm'] = self.summary_llm

            if self.llm or self.summary_llm:
                logger.info("Using paper-qa with llm=%s, summary_llm=%s",
                           self.llm or 'default', self.summary_llm or 'default')

            # ============================================================
            # STEP 3: Run paper-qa ask() asynchronously
            # ============================================================
            # Paper-qa's ask() is async, so we need to run it in an
            # event loop. We handle two cases:
            # - Normal: use asyncio.run() to create a new loop
            # - Jupyter/nested: spawn a thread with its own loop
            async def _run_async() -> Any:
                settings = self._settings_class(**settings_kwargs)
                return await self._ask_func(question, settings=settings)

            try:
                # Primary path: run in current thread with new event loop
                ans_obj = asyncio.run(_run_async())
            except RuntimeError as exc:
                if "event loop" not in str(exc).lower():
                    raise
                # Fallback: event loop already running (Jupyter, etc.)
                # Spawn a thread with its own event loop
                outcome: Dict[str, Any] = {}
                error: Dict[str, BaseException] = {}

                def _worker() -> None:
                    """Background thread worker to run async code."""
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        outcome['value'] = new_loop.run_until_complete(_run_async())
                    except BaseException as exc:
                        error['error'] = exc
                    finally:
                        asyncio.set_event_loop(None)
                        new_loop.close()

                thread = threading.Thread(target=_worker, daemon=True)
                thread.start()
                thread.join()

                if 'error' in error:
                    raise error['error']
                ans_obj = outcome.get('value')

            # ============================================================
            # STEP 4: Extract answer string from response object
            # ============================================================
            logger.debug(f"paper-qa returned type: {type(ans_obj).__name__}")
            return self._extract_answer(ans_obj)

        except Exception as e:
            logger.error(f"paperqa query failed for {pdf_path}: {e}")
            return None
        finally:
            # ============================================================
            # STEP 5: Clean up per-PDF directories
            # ============================================================
            try:
                if os.path.exists(paper_dir):
                    shutil.rmtree(paper_dir)
                if os.path.exists(index_dir):
                    shutil.rmtree(index_dir)
                logger.debug("Cleaned up per-PDF paper/index dirs")
            except Exception as e:
                logger.warning(f"Failed to clean up per-PDF dirs: {e}")

    @staticmethod
    def _extract_answer(ans_obj: Any) -> Optional[str]:
        """Extract a clean answer string from paper-qa's response object.

        Paper-qa returns various object types depending on version and
        configuration. This method tries multiple extraction strategies
        to get a usable string answer:

        1. Direct string: return as-is
        2. Known attributes: raw_answer, answer, formatted_answer, etc.
        3. Pydantic model: use model_dump() and extract from dict
        4. get_summary() method: call if available
        5. str() conversion: last resort fallback

        Parameters
        ----------
        ans_obj : Any
            The response object from paper-qa's ask() function.

        Returns
        -------
        str or None
            Extracted answer string, or None if extraction failed.

        Note
        ----
        We prioritize 'raw_answer' over 'answer' because 'answer' may
        contain formatted text with citations, while 'raw_answer' is
        more likely to be the clean JSON we requested.
        """
        # Direct string case
        if isinstance(ans_obj, str) and ans_obj.strip():
            return ans_obj.strip()

        # PRIORITY: Try to get raw answer before formatted versions
        for attr in ("raw_answer", "answer"):
            try:
                val = getattr(ans_obj, attr, None)
                if callable(val):
                    try:
                        val = val()
                    except Exception:
                        continue
                if isinstance(val, str) and val.strip():
                    if val.strip().startswith('{') or re.search(r'\{["\s]*summary["\s]*:', val):
                        logger.debug(f"Extracted RAW answer from attribute '{attr}'")
                        return val.strip()
            except Exception:
                continue

        # Handle Pydantic models
        if hasattr(ans_obj, 'model_dump'):
            try:
                data = ans_obj.model_dump()
                if 'session' in data and isinstance(data['session'], dict):
                    session = data['session']
                    for key in ('answer', 'formatted_answer', 'response', 'content', 'text', 'raw_answer'):
                        if key in session and isinstance(session[key], str) and session[key].strip():
                            return session[key].strip()
                for key in ('answer', 'formatted_answer', 'response', 'content', 'text'):
                    if key in data and isinstance(data[key], str) and data[key].strip():
                        return data[key].strip()
            except Exception:
                pass

        # Try get_summary() method
        if hasattr(ans_obj, 'get_summary') and callable(getattr(ans_obj, 'get_summary', None)):
            try:
                summary = ans_obj.get_summary()
                if inspect.iscoroutine(summary):
                    pass  # Can't await in sync context
                elif isinstance(summary, str) and summary.strip():
                    json_match = re.search(r'\{["\s]*summary["\s]*:', summary)
                    if json_match:
                        return summary[json_match.start():].strip()
                    return summary.strip()
            except Exception:
                pass

        # Try common attributes
        for attr in ("answer", "formatted_answer", "content", "text", "response"):
            try:
                val = getattr(ans_obj, attr, None)
                if callable(val):
                    try:
                        val = val()
                    except Exception:
                        continue
                if isinstance(val, str) and val.strip():
                    return val.strip()
            except Exception:
                continue

        # Last resort: convert to string
        try:
            s = str(ans_obj).strip()
            if s and len(s) < 10000:
                return s if s else None
        except Exception:
            return None

        return None


def _call_paperqa_on_pdf(
    pdf_path: str,
    *,
    question: str,
    llm: Optional[str] = None,
    summary_llm: Optional[str] = None,
) -> Optional[str]:
    """Summarize a PDF using paper-qa if available; return raw string answer.

    Args:
        pdf_path: Path to the PDF file to summarize.
        question: The question/prompt to ask paper-qa about the PDF.
        llm: LLM model for paper-qa (e.g., 'gpt-4o', 'gpt-5.2'). If None, uses paper-qa default.
        summary_llm: Summary LLM model. If None, uses paper-qa default.
    """
    import tempfile
    import pathlib

    # CRITICAL: Create temp directory and set PQA_HOME BEFORE importing paperqa
    # Paper-qa reads PQA_HOME at import time and caches it, so we must set it first
    temp_dir = tempfile.mkdtemp(prefix='paperqa_')
    temp_index_dir = os.path.join(temp_dir, 'index')
    os.makedirs(temp_index_dir, exist_ok=True)

    # Save original environment to restore later
    original_cwd = os.getcwd()
    original_pqa_home = os.environ.get('PQA_HOME')

    # Set PQA_HOME BEFORE importing paperqa
    os.environ['PQA_HOME'] = temp_dir
    logger.debug(f"Set PQA_HOME to: {temp_dir} (before paperqa import)")

    # Change to temp directory BEFORE importing paperqa
    os.chdir(temp_dir)
    logger.debug(f"Changed working directory to: {temp_dir} (before paperqa import)")

    try:
        from paperqa import Settings, ask  # type: ignore
    except Exception as e:
        logger.error("paperqa not installed or import failed: %s", e)
        # Restore environment before returning
        _restore_environment(original_cwd, original_pqa_home, temp_dir)
        return None

    # Configure litellm for GPT-5 compatibility
    # GPT-5 models only support temperature=1, so we need to handle unsupported params
    try:
        import litellm
        # Check if using GPT-5 model
        is_gpt5 = (llm and 'gpt-5' in llm.lower()) or (summary_llm and 'gpt-5' in summary_llm.lower())
        if is_gpt5:
            # Enable dropping unsupported params for GPT-5 models
            litellm.drop_params = True
            logger.info("Enabled litellm.drop_params for GPT-5 model compatibility")
    except Exception as e:
        logger.debug("Could not configure litellm: %s", e)

    def _extract_answer(ans_obj: Any) -> Optional[str]:
        """Normalize paper-qa answer objects down to a clean string, if present."""
        # Direct string case
        if isinstance(ans_obj, str) and ans_obj.strip():
            return ans_obj.strip()

        # PRIORITY: Try to get raw answer before formatted versions (for GPT-5.2 compatibility)
        # Paper-qa's get_summary() adds headers, so try raw_answer/answer first
        for attr in ("raw_answer", "answer"):
            try:
                val = getattr(ans_obj, attr, None)
                if callable(val):
                    try:
                        val = val()
                    except Exception:
                        continue
                if isinstance(val, str) and val.strip():
                    # Check if this looks like raw JSON (without headers)
                    if val.strip().startswith('{') or re.search(r'\{["\s]*summary["\s]*:', val):
                        logger.debug(f"Extracted RAW answer from attribute '{attr}' (bypassing formatting)")
                        return val.strip()
            except Exception as e:
                logger.debug(f"Failed to access attribute '{attr}': {e}")
                continue

        # Handle Pydantic models (AnswerResponse, PQASession, etc.)
        # Try Pydantic-specific methods first
        if hasattr(ans_obj, 'model_dump'):
            try:
                data = ans_obj.model_dump()
                logger.debug(f"Pydantic model_dump() returned keys: {list(data.keys())}")

                # AnswerResponse typically has a 'session' field containing the actual session data
                if 'session' in data and isinstance(data['session'], dict):
                    session = data['session']
                    logger.debug(f"Found session dict with keys: {list(session.keys())[:10]}")
                    # Look for answer in session
                    for key in ('answer', 'formatted_answer', 'response', 'content', 'text', 'raw_answer'):
                        if key in session and isinstance(session[key], str) and session[key].strip():
                            logger.debug(f"Extracted answer from session['{key}']")
                            return session[key].strip()

                # Try common answer field names in the dumped dict
                for key in ('answer', 'formatted_answer', 'response', 'content', 'text'):
                    if key in data and isinstance(data[key], str) and data[key].strip():
                        logger.debug(f"Extracted answer from Pydantic model via model_dump()['{key}']")
                        return data[key].strip()

                # If no direct hit, look for any field containing JSON-like content
                for key, value in data.items():
                    if isinstance(value, str) and value.strip():
                        # Check if this looks like our expected JSON format
                        if re.search(r'\{["\s]*summary["\s]*:', value):
                            logger.debug(f"Found JSON-like content in model_dump()['{key}']")
                            return value.strip()
                    # Also check nested dicts (like session)
                    elif isinstance(value, dict):
                        for subkey, subvalue in value.items():
                            if isinstance(subvalue, str) and subvalue.strip():
                                if re.search(r'\{["\s]*summary["\s]*:', subvalue):
                                    logger.debug(f"Found JSON-like content in model_dump()['{key}']['{subkey}']")
                                    return subvalue.strip()
            except Exception as e:
                logger.warning(f"Failed to extract from model_dump(): {e}")

        # Try get_summary() method (common in paper-qa AnswerResponse)
        # Note: get_summary() may be async (coroutine) in newer paper-qa versions
        if hasattr(ans_obj, 'get_summary') and callable(getattr(ans_obj, 'get_summary', None)):
            try:
                summary = ans_obj.get_summary()
                # Check if it's a coroutine (async method) - we can't await it in sync context
                if inspect.iscoroutine(summary):
                    logger.debug("get_summary() returned coroutine (async method), skipping")
                elif isinstance(summary, str) and summary.strip():
                    logger.debug("Extracted answer from get_summary() method")
                    # Check if it's a formatted string with "Answer: " prefix or similar
                    # Try to extract just the JSON part if present
                    json_match = re.search(r'\{["\s]*summary["\s]*:', summary)
                    if json_match:
                        # Found JSON starting with "summary" field
                        # Try to extract the complete JSON object
                        json_start = json_match.start()
                        logger.debug("Extracted JSON from formatted summary")
                        return summary[json_start:].strip()
                    return summary.strip()
            except Exception as e:
                logger.debug(f"Failed to call get_summary(): {e}")

        # Try extracting from common attributes directly
        for attr in ("answer", "formatted_answer", "content", "text", "response", "raw_answer"):
            try:
                val = getattr(ans_obj, attr, None)
                # Handle callable attributes (properties)
                if callable(val):
                    try:
                        val = val()
                    except Exception:
                        continue
                if isinstance(val, str) and val.strip():
                    logger.debug(f"Extracted answer from attribute '{attr}'")
                    return val.strip()
            except Exception as e:
                logger.debug(f"Failed to access attribute '{attr}': {e}")
                continue

        # If we have an object that looks like a session/response, log its type and available attributes
        obj_type = type(ans_obj).__name__
        attrs = [a for a in dir(ans_obj) if not a.startswith('_')]
        logger.warning(
            f"Could not extract clean answer from {obj_type}. "
            f"Available attributes: {attrs[:30]}"
        )

        # Debug: try to show sample content from each string attribute
        logger.debug("Sample content from string attributes:")
        for attr in attrs[:15]:
            try:
                val = getattr(ans_obj, attr, None)
                if callable(val):
                    continue
                if isinstance(val, str) and val:
                    logger.debug(f"  {attr}: {val[:100]}...")
            except Exception:
                pass

        # Last resort: convert to string but warn about it
        try:
            s = str(ans_obj).strip()
            if s and len(s) < 10000:  # Only if not too large
                # Check if it looks like a repr() dump (contains object address)
                if ' at 0x' in s or 'object at ' in s:
                    logger.error(f"Falling back to str() representation of {obj_type}, this may not be clean")
                return s if s else None
        except Exception as e:
            logger.error(f"Failed to convert answer object to string: {e}")
            return None

    # Copy PDF to isolated temporary directory (temp_dir was created above, before import)
    temp_pdf_path = os.path.join(temp_dir, os.path.basename(pdf_path))

    try:
        shutil.copy2(pdf_path, temp_pdf_path)
        logger.debug(f"Processing PDF in isolated directory: {temp_dir}")

        # Build Settings with configured LLM models and BOTH isolated directories
        # paper_directory: where to look for papers
        # index_directory: where to store the Tantivy search index
        settings_kwargs: Dict[str, Any] = {
            'paper_directory': pathlib.Path(temp_dir),
            'index_directory': pathlib.Path(temp_index_dir),
        }
        if llm:
            settings_kwargs['llm'] = llm
        if summary_llm:
            settings_kwargs['summary_llm'] = summary_llm

        if llm or summary_llm:
            logger.info("Using paper-qa with llm=%s, summary_llm=%s", llm or 'default', summary_llm or 'default')

        async def _run_async() -> Any:
            """Run the paper-qa pipeline using the ask() API."""
            settings = Settings(**settings_kwargs)
            return await ask(question, settings=settings)

        try:
            try:
                ans_obj = asyncio.run(_run_async())
                logger.debug(f"paper-qa returned type: {type(ans_obj).__name__}")
                return _extract_answer(ans_obj)
            except RuntimeError as exc:
                if "event loop" not in str(exc).lower():
                    raise

            outcome: Dict[str, Any] = {}
            error: Dict[str, BaseException] = {}

            def _worker() -> None:
                """Bridge paper-qa async calls into a background event loop."""
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

            ans_obj = outcome.get('value')
            logger.debug(f"paper-qa returned type (via thread): {type(ans_obj).__name__}")
            return _extract_answer(ans_obj)
        except Exception as e:
            logger.error("paperqa query failed for %s: %s", pdf_path, e)
            return None
    finally:
        _restore_environment(original_cwd, original_pqa_home, temp_dir)


def _normalize_summary_json(raw: str) -> Optional[str]:
    """Strip code fences, parse JSON, and ensure required keys exist.

    - Removes leading ```/```json and trailing ``` fences when present
    - Ensures keys: summary, methods (accepts aliases such as method/approach)
    - On parse failure, wraps cleaned text under 'summary' and sets 'methods' to ''
    """
    import json

    def _strip_fences(s: str) -> str:
        """Remove Markdown code fences from LLM output, preserving inner text."""
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
        """Parse a JSON object string, returning None on failure or non-dict values."""
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            # GPT-5.2 sometimes returns JSON with unescaped newlines in strings
            # Try fixing by escaping newlines within quoted strings
            try:
                # This is a simple heuristic: escape literal \n characters
                # but only if they appear to be inside JSON string values
                import re
                # Find all string values in JSON and escape their newlines
                def escape_newlines_in_strings(match):
                    s = match.group(0)
                    # Escape newlines and tabs in the string value
                    return s.replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')

                # Match JSON string values: "..." (handling escaped quotes)
                pattern = r'"(?:[^"\\]|\\.)*"'
                fixed = re.sub(pattern, escape_newlines_in_strings, txt)
                obj = json.loads(fixed)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
        except Exception:
            return None

    def _coerce_str(v) -> str:
        """Coerce values into strings while tolerating None."""
        if v is None:
            return ''
        return v if isinstance(v, str) else str(v)

    # Strip paper-qa formatting headers (e.g., "Fulltext summary\nSummary\n")
    cleaned = _strip_fences(raw)
    # Remove common paper-qa headers
    for header in ['Fulltext summary', 'Summary', 'Methods', 'Answer']:
        lines = cleaned.split('\n')
        cleaned = '\n'.join(line for line in lines if line.strip() != header)
    cleaned = cleaned.strip()

    # First try: parse as-is
    data = _parse_obj(cleaned)

    if data is None:
        # Second try: extract JSON between outermost braces
        s = cleaned
        start = s.find('{')
        end = s.rfind('}')
        if start != -1 and end != -1 and end > start:
            potential_json = s[start:end+1]
            data = _parse_obj(potential_json)

            # If parse failed, try unescaping quotes (handles `\"methods\":\"...\"` case)
            if data is None and '\\\"' in potential_json:
                try:
                    # Fix escaped quotes inside the JSON
                    unescaped = potential_json.replace('\\\"', '"')
                    # But this might create invalid JSON with nested quotes
                    # Try a more targeted fix: look for the pattern `"summary":"...\"methods\"..."`
                    # and split it into proper top-level keys
                    if '"summary":"' in unescaped and '","methods":"' not in unescaped and '"methods":"' in unescaped:
                        # Pattern: {"summary":"... "methods":"..."} where methods is inside summary
                        # Find where "methods" starts within the summary value
                        import re
                        # Use non-greedy match but handle the closing brace properly
                        match = re.search(r'"summary"\s*:\s*"(.*?)\s*"\s*"methods"\s*:\s*"\s*"(.*?)"\s*}', unescaped, re.DOTALL)
                        if not match:
                            # Try alternative pattern without extra quotes
                            match = re.search(r'"summary"\s*:\s*"(.*?)\\n\\n"methods"\s*:\s*"(.*?)"', unescaped, re.DOTALL)
                        if match:
                            summary_val = match.group(1).strip()
                            methods_val = match.group(2).strip()
                            # Clean up escape sequences
                            summary_val = summary_val.replace('\\n', '\n').replace('\\t', '\t')
                            methods_val = methods_val.replace('\\n', '\n').replace('\\t', '\t')
                            data = {'summary': summary_val, 'methods': methods_val}
                except Exception:
                    pass

    if data is None:
        # Last resort: wrap everything as summary
        out = {
            'summary': cleaned.strip(),
            'methods': '',
        }
        return json.dumps(out, ensure_ascii=False)

    def _first_nonempty(keys: Tuple[str, ...]) -> str:
        """Return the first non-empty string value from data for the provided keys."""
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
        with db.get_connection('history', row_factory=False) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matched_entries SET paper_qa_summary = ? WHERE entry_id = ?", (json_summary, entry_id))
            updated_history = cur.rowcount
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

    Workflow overview
    -----------------

    - Load configuration/database state and prepare download/archive folders.
    - Determine targets either from ranked topic entries (respecting the download
      rank threshold and optional ``limit``) or from the explicit ``arxiv``/``entry_ids``
      arguments, optionally pulling metadata from the history database when
      ``use_history`` is enabled.
    - Resolve arXiv IDs, reuse archived PDFs when possible, download missing
      PDFs under the configured rate limit, and archive successful downloads.
    - Run ``paper-qa`` on each PDF, normalize the JSON result, and write summaries
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

    topics = resolve_topics(cfg_mgr, topic)
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

    # Extract LLM model settings from config
    pqa_llm = paperqa_cfg.get('llm') or None
    pqa_summary_llm = paperqa_cfg.get('summary_llm') or None

    summarized = 0

    # Use PaperQASession to set up environment ONCE for all PDFs
    # This avoids Python import caching issues where paperqa caches PQA_HOME
    with PaperQASession(llm=pqa_llm, summary_llm=pqa_summary_llm) as pqa_session:
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

            # Use session to process this PDF
            raw_ans = pqa_session.summarize_pdf(pdf_path, question)

            if not raw_ans:
                logger.warning("No answer returned from paper-qa for arXiv:%s (entry_id=%s)", aid, eid or "-")
                continue

            # Output the raw paper-qa response for inspection
            try:
                logger.info("=" * 80)
                logger.info("Paper-QA Summary for arXiv:%s (entry_id=%s)", aid, eid or "-")
                logger.info("Model: llm=%s, summary_llm=%s", pqa_llm or 'default', pqa_summary_llm or 'default')
                logger.info("=" * 80)
                logger.info("RAW ANSWER (first 500 chars):\n%s", raw_ans[:500] if raw_ans else "None")
                logger.info("=" * 80)
            except Exception as e:
                # Best-effort logging; ignore formatting failures
                logger.debug("Failed to log paper-qa response: %s", e)

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
