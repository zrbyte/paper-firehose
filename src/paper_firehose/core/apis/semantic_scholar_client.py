"""
Semantic Scholar API client for fetching paper abstracts.

Semantic Scholar provides free access to academic paper metadata including
abstracts without requiring an API key.
"""

from __future__ import annotations

from urllib.parse import quote
from typing import Optional

import requests

from ..http_client import RetryableHTTPClient
from ..text_utils import strip_jats


def get_semantic_scholar_abstract(
    doi: str,
    *,
    session: Optional[requests.Session] = None
) -> Optional[str]:
    """Fetch abstract from Semantic Scholar Graph API by DOI (no key needed).

    Args:
        doi: Digital Object Identifier to look up
        session: Optional requests.Session for backward compatibility

    Returns:
        Plain-text abstract or None if not available
    """
    if not doi:
        return None

    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi)}?fields=abstract"

    # If session is provided, use old logic for compatibility
    if session:
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            abs_txt = data.get('abstract')
            return strip_jats(abs_txt) if abs_txt else None
        except Exception:
            return None

    # Use new RetryableHTTPClient for better retry logic
    try:
        client = RetryableHTTPClient(rps=1.0, max_retries=3)
        r = client.get_with_retry(url)
        if r is None:  # 404 case
            return None

        data = r.json()
        abs_txt = data.get('abstract')
        return strip_jats(abs_txt) if abs_txt else None
    except Exception:
        return None
