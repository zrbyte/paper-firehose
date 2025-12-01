"""
Crossref API client for fetching paper abstracts.

Crossref is a major DOI registration agency with comprehensive metadata
including abstracts for academic publications.
"""

from __future__ import annotations

import time
from urllib.parse import quote
from typing import Optional

import requests

from ..http_client import RetryableHTTPClient
from ..text_utils import strip_jats


CROSSREF_API = "https://api.crossref.org/works/"


def get_crossref_abstract(
    doi: str,
    *,
    mailto: str,
    max_retries: int = 3,
    session: Optional[requests.Session] = None
) -> Optional[str]:
    """Return the plain-text abstract for DOI or None if not available.

    Implements exponential backoff on 429/5xx and honors Retry-After when present.
    Also sends Crossref the mailto parameter.

    Args:
        doi: Digital Object Identifier to look up
        mailto: Contact email for Crossref User-Agent
        max_retries: Maximum number of retry attempts (default: 3)
        session: Optional requests.Session for backward compatibility

    Returns:
        Plain-text abstract or None if not available
    """
    # If session is provided, use old logic for compatibility
    if session:
        url = f"{CROSSREF_API}{quote(doi)}?mailto={quote(mailto)}"
        headers = {
            "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
        }
        for attempt in range(max_retries):
            try:
                r = session.get(url, headers=headers, timeout=15)
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
                    return strip_jats(abstract) or None
                return None
            except Exception:
                time.sleep(min(8.0, 2.0 ** attempt))
                continue
        return None

    # Use new RetryableHTTPClient for better retry logic
    url = f"{CROSSREF_API}{quote(doi)}?mailto={quote(mailto)}"
    headers = {
        "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
    }

    try:
        client = RetryableHTTPClient(rps=1.0, max_retries=max_retries)
        r = client.get_with_retry(url, headers=headers)
        if r is None:  # 404 case
            return None

        data = r.json()
        msg = data.get("message", {})
        abstract = msg.get("abstract")
        if abstract:
            return strip_jats(abstract) or None
        return None
    except Exception:
        return None


def search_crossref_abstract_by_title(
    title: str,
    *,
    mailto: str,
    max_retries: int = 2,
    session: Optional[requests.Session] = None
) -> Optional[str]:
    """Best-effort abstract lookup by title when DOI is missing or returns no abstract.

    Uses Crossref's works search endpoint with a bibliographic query. Returns the
    first item's abstract if available.

    Args:
        title: Paper title to search for
        mailto: Contact email for Crossref User-Agent
        max_retries: Maximum number of retry attempts (default: 2)
        session: Optional requests.Session for backward compatibility

    Returns:
        Plain-text abstract or None if not available
    """
    if not title:
        return None

    # If session is provided, use old logic for compatibility
    if session:
        base = "https://api.crossref.org/works"
        params = f"?query.bibliographic={quote(title)}&rows=1&mailto={quote(mailto)}"
        url = base + params
        headers = {
            "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
        }
        for attempt in range(max_retries):
            try:
                r = session.get(url, headers=headers, timeout=15)
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
                        return strip_jats(abstract) or None
                return None
            except Exception:
                time.sleep(min(8.0, 2.0 ** attempt))
                continue
        return None

    # Use new RetryableHTTPClient for better retry logic
    base = "https://api.crossref.org/works"
    params = f"?query.bibliographic={quote(title)}&rows=1&mailto={quote(mailto)}"
    url = base + params
    headers = {
        "User-Agent": f"paper-firehose/abstract-fetcher (mailto:{mailto})"
    }

    try:
        client = RetryableHTTPClient(rps=1.0, max_retries=max_retries)
        r = client.get_with_retry(url, headers=headers)
        if r is None:  # 404 case
            return None

        data = r.json()
        items = (data.get('message') or {}).get('items') or []
        if items:
            abstract = items[0].get('abstract')
            if abstract:
                return strip_jats(abstract) or None
        return None
    except Exception:
        return None
