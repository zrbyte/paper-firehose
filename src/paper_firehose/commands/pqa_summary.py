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
            "All topics must define paper-qa settings."
        )

    logger.info("Loaded paperqa config for topic '%s'", topic_name)
    return topic_pqa


def _build_paperqa_settings_kwargs(
    settings_cls: type,
    *,
    llm: Optional[str],
    summary_llm: Optional[str],
) -> Dict[str, Any]:
    """Build Settings kwargs for Docs.aquery() — no agent, no file paths needed."""
    fields = getattr(settings_cls, "model_fields", None)
    if fields is None:
        fields = getattr(settings_cls, "__fields__", None)
    field_names = set(fields.keys()) if fields else set()

    settings_kwargs: Dict[str, Any] = {}

    if llm:
        settings_kwargs["llm"] = llm
    if summary_llm:
        settings_kwargs["summary_llm"] = summary_llm

    # GPT-5 models only support temperature=1; paperqa defaults to 0.0 which
    # causes a 400 BadRequestError. Override at Settings level.
    is_gpt5 = (llm and 'gpt-5' in llm.lower()) or \
              (summary_llm and 'gpt-5' in summary_llm.lower())
    if is_gpt5 and "temperature" in field_names:
        settings_kwargs["temperature"] = 1.0

    # Disable vision API / media enrichment.
    # PDF figures are often extracted in formats (BMP/TIFF) that OpenAI rejects,
    # causing floods of BadRequestError retries and chunk failures.
    # paper-qa has changed this API across versions:
    #   - New (>=2026.x): ParsingSettings.multimodal (default=ON_WITH_ENRICHMENT=1) — set to False
    #   - Older (5.x with parse_pdf_tables_and_figures field): set to False
    if "parsing" in field_names:
        parsing_cfg: Dict[str, Any] = {"use_doc_details": False}
        try:
            from paperqa.settings import ParsingSettings as _ParsingSettings  # type: ignore[import]
            ps_fields = (
                getattr(_ParsingSettings, "model_fields", None)
                or getattr(_ParsingSettings, "__fields__", None)
            )
            if ps_fields:
                if "multimodal" in ps_fields:
                    # paper-qa >=2026.x: multimodal controls image/table extraction
                    parsing_cfg["multimodal"] = False
                    logger.debug("Disabled multimodal enrichment in ParsingSettings")
                elif "parse_pdf_tables_and_figures" in ps_fields:
                    # Older paper-qa with explicit figure extraction field
                    parsing_cfg["parse_pdf_tables_and_figures"] = False
                    logger.debug("Disabled parse_pdf_tables_and_figures in ParsingSettings")
                else:
                    logger.warning(
                        "Neither 'multimodal' nor 'parse_pdf_tables_and_figures' found "
                        "in ParsingSettings; figure extraction may cause image format errors"
                    )
            else:
                logger.warning("Could not read ParsingSettings fields; figure extraction may cause errors")
        except Exception as exc:
            logger.warning("Could not check ParsingSettings for figure extraction field: %s", exc)
        settings_kwargs["parsing"] = parsing_cfg

    # Disable JSON-format chunk summaries.
    # With use_json=True (default), paper-qa asks summary_llm to return JSON with
    # "summary" and "relevance_score" for each chunk. Models that omit relevance_score
    # (e.g. gpt-4o-mini) trigger LLMBadContextJSONError on every chunk.
    # use_json=False switches to plain-text summaries where the score is simply an
    # integer on its own line — simpler and universally supported.
    if "prompts" in field_names:
        settings_kwargs["prompts"] = {"use_json": False}
        logger.debug("Disabled JSON chunk summaries (use_json=False) to avoid score extraction errors")

    return settings_kwargs


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
        self._docs_class: Optional[type] = None
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
            from paperqa import Settings, Docs
            self._settings_class = Settings
            self._docs_class = Docs
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
        # ============================================================
        # Validate session state
        # ============================================================
        if not self._initialized:
            logger.error("PaperQASession not initialized")
            return None

        if self.llm or self.summary_llm:
            logger.info("Using paper-qa (Docs API) with llm=%s, summary_llm=%s",
                        self.llm or 'default', self.summary_llm or 'default')

        try:
            # ============================================================
            # Build Settings (no agent, no file-path settings needed)
            # ============================================================
            settings_kwargs = _build_paperqa_settings_kwargs(
                self._settings_class,
                llm=self.llm,
                summary_llm=self.summary_llm,
            )
            settings = self._settings_class(**settings_kwargs)

            # ============================================================
            # Run Docs.aadd + Docs.aquery asynchronously
            # Docs manages its own in-memory vector store; each call gets
            # a fresh Docs instance so there is no cross-PDF contamination.
            # ============================================================
            async def _run_async() -> Any:
                docs = self._docs_class()
                await docs.aadd(pdf_path, settings=settings)
                return await docs.aquery(question, settings=settings)

            try:
                ans_obj = asyncio.run(_run_async())
            except RuntimeError as exc:
                if "event loop" not in str(exc).lower():
                    raise
                # Fallback for Jupyter / nested event loops
                outcome: Dict[str, Any] = {}
                error: Dict[str, BaseException] = {}

                def _worker() -> None:
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        outcome['value'] = new_loop.run_until_complete(_run_async())
                    except BaseException as exc:
                        error['error'] = exc
                    finally:
                        asyncio.set_event_loop(None)
                        new_loop.close()

                thread = threading.Thread(target=_worker)
                thread.start()
                thread.join(timeout=300)

                if thread.is_alive():
                    logger.error("paper-qa thread timed out after 300s for %s", pdf_path)
                    return None

                if 'error' in error:
                    raise error['error']
                ans_obj = outcome.get('value')

            # ============================================================
            # Extract answer — PQASession.answer is the clean answer string
            # ============================================================
            if ans_obj is None:
                return None
            answer = getattr(ans_obj, 'answer', None) or getattr(ans_obj, 'raw_answer', None)
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            logger.error("paperqa returned %s with no usable .answer for %s", type(ans_obj).__name__, pdf_path)
            return None

        except Exception as e:
            logger.error(f"paperqa query failed for {pdf_path}: {e}")
            return None


def _normalize_summary_json(raw: str) -> Optional[str]:
    """Strip code fences, parse JSON, and ensure ``summary`` and ``methods`` keys.

    Processing steps:

    1. Remove Markdown code fences (````` or `````json`).
    2. Strip standalone paper-qa section headers that may precede the JSON.
    3. Parse JSON directly, falling back to extracting the outermost ``{…}``.
    4. If parsing fails, wrap the cleaned text as a plain-text ``summary``.
    """
    import json

    if not raw or not raw.strip():
        return None

    # -- Strip Markdown code fences --
    cleaned = raw.strip()
    if cleaned.startswith('```'):
        nl = cleaned.find('\n')
        cleaned = cleaned[nl + 1:] if nl != -1 else cleaned.lstrip('`')
    cleaned = cleaned.rstrip()
    if cleaned.endswith('```'):
        cleaned = cleaned[:-3].rstrip()

    # -- Remove standalone paper-qa section headers --
    cleaned = '\n'.join(
        line for line in cleaned.split('\n')
        if line.strip() not in ('Fulltext summary', 'Summary', 'Methods', 'Answer')
    ).strip()

    # -- Try to parse JSON --
    def _try_parse(txt: str) -> Optional[dict]:
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    data = _try_parse(cleaned)
    if data is None:
        # Fallback: extract outermost {…} block
        start, end = cleaned.find('{'), cleaned.rfind('}')
        if start != -1 and end > start:
            data = _try_parse(cleaned[start:end + 1])

    if data is None:
        return json.dumps({'summary': cleaned, 'methods': ''}, ensure_ascii=False)

    def _str(v) -> str:
        if v is None:
            return ''
        return v if isinstance(v, str) else str(v)

    summary_val = _str(data.get('summary')).strip()
    if not summary_val:
        summary_val = cleaned

    methods_val = _str(data.get('methods')).strip()

    return json.dumps({'summary': summary_val, 'methods': methods_val}, ensure_ascii=False)


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
    except sqlite3.Error as e:
        logger.debug("Failed to write to papers.db for %s: %s", entry_id, e)
    # History DB
    try:
        with db.get_connection('history', row_factory=False) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matched_entries SET paper_qa_summary = ? WHERE entry_id = ?", (json_summary, entry_id))
            updated_history = cur.rowcount
            logger.info("paper-qa DB write (history.db): entry_id=%s updated_rows=%d", entry_id, updated_history)
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

    _ensure_dirs(download_dir, archive_dir)

    topics = resolve_topics(cfg_mgr, topic)
    mailto = _resolve_mailto(config)

    downloaded_paths: List[str] = []
    summarize_targets: List[Tuple[Optional[str], str, str, Optional[str]]] = []  # (entry_id, arxiv_id, pdf_path, topic_ctx)
    topic_cfg_cache: Dict[str, Dict[str, Any]] = {}  # topic name -> loaded topic config
    sess = requests.Session()

    total_candidates = 0
    total_downloaded = 0

    # Manual mode: download specific arXiv IDs/URLs if provided
    if arxiv:
        # Manual mode: use hardcoded defaults since no topic specified
        DEFAULT_MAX_RETRIES = 3
        DEFAULT_RPS = 0.3
        min_interval_default = max(3.0, 1.0 / max(DEFAULT_RPS, 0.01))

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
            ok = _download_pdf(pdf_url, dest_path, mailto=mailto, session=sess, max_retries=DEFAULT_MAX_RETRIES)
            if ok:
                downloaded_paths.append(dest_path)
                summarize_targets.append((None, arxiv_id, dest_path, None))
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
        # History mode: use hardcoded defaults since entries span multiple topics
        DEFAULT_MAX_RETRIES = 3
        DEFAULT_RPS = 0.3
        min_interval_default = max(3.0, 1.0 / max(DEFAULT_RPS, 0.01))

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
        max_retries_topic = int(paperqa_cfg_topic.get('max_retries', 3))
        rps_topic = float(paperqa_cfg_topic.get('rps', 0.3))
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
    logger.info("Completed pqa_summary: candidates=%d, downloaded=%d, archived=%d", total_candidates, total_downloaded, len(downloaded_paths))

    if not summarize_targets:
        logger.info("No PDFs to summarize.")
        _cleanup_archive(archive_dir)
        return

    summarized = 0

    # Group summarize_targets by topic to batch by LLM models
    from collections import defaultdict
    targets_by_topic: Dict[Optional[str], List[Tuple]] = defaultdict(list)
    for eid, aid, pdf_path, tctx in summarize_targets:
        targets_by_topic[tctx].append((eid, aid, pdf_path, tctx))

    # Process each topic's PDFs with appropriate LLM models
    for topic_name, targets in targets_by_topic.items():
        if not topic_name:
            logger.warning("Skipping %d PDFs with no topic context", len(targets))
            continue

        # Reuse topic config loaded during download phase; fall back to fresh load if missing
        # (e.g. when targets come from --arxiv/--entry-ids with an explicit --topic)
        try:
            topic_cfg = topic_cfg_cache.get(topic_name) or cfg_mgr.load_topic_config(topic_name)
            paperqa_cfg = _get_topic_paperqa_config(topic_cfg, topic_name)
        except Exception as e:
            logger.error("Failed to load paperqa config for '%s': %s. Skipping.", topic_name, e)
            continue

        pqa_llm = paperqa_cfg.get('llm')
        pqa_summary_llm = paperqa_cfg.get('summary_llm')

        logger.info("Processing %d PDFs for topic '%s' with llm=%s", len(targets), topic_name, pqa_llm)

        # Create session with topic-specific LLM models
        with PaperQASession(llm=pqa_llm, summary_llm=pqa_summary_llm) as pqa_session:
            for eid, aid, pdf_path, tctx in targets:
                # Get prompt from topic's paperqa config
                question = (paperqa_cfg.get('prompt') or '').strip()
                if not question:
                    question = (
                        "What are the main scientific findings, methods, and experimental details "
                        "of the indexed paper? Return ONLY a JSON object: "
                        "{\"summary\": \"...\", \"methods\": \"...\"}. "
                        "summary: up to 8 information-dense sentences on findings and contributions. "
                        "methods: experimental setup, parameters, analysis methods, calculation details, tool names."
                    )

                # Apply {ranking_query} placeholder substitution
                if '{ranking_query}' in question:
                    rq = ((topic_cfg.get('ranking') or {}).get('query') or '').strip()
                    if rq:
                        question = question.replace('{ranking_query}', rq)

                # Use session to process this PDF
                raw_ans = pqa_session.summarize_pdf(pdf_path, question)

                if not raw_ans:
                    logger.warning("No answer returned from paper-qa for arXiv:%s (entry_id=%s)", aid, eid or "-")
                    continue

                _raw_lower = raw_ans.strip().lower()
                if 'i cannot answer' in _raw_lower or _raw_lower == 'no answer generated.':
                    logger.warning(
                        "paper-qa returned unusable answer (%r) for arXiv:%s (entry_id=%s); skipping DB write",
                        raw_ans[:50], aid, eid or "-",
                    )
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
