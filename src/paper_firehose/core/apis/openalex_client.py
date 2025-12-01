"""
OpenAlex API client for fetching paper abstracts.

OpenAlex is an open catalog of scholarly papers that provides metadata including
abstracts, sometimes in an inverted-index format that needs reconstruction.
"""

from __future__ import annotations

from urllib.parse import quote
from typing import Optional, Dict, Any

import requests

from ..http_client import RetryableHTTPClient
from ..text_utils import strip_jats


def _reconstruct_openalex(ii: Dict[str, Any]) -> Optional[str]:
    """Reassemble OpenAlex's inverted-index abstract representation.

    Args:
        ii: Inverted index dictionary mapping words to position lists

    Returns:
        Reconstructed abstract text or None if reconstruction fails
    """
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


def get_openalex_abstract(
    doi: str,
    *,
    mailto: str,
    session: Optional[requests.Session] = None
) -> Optional[str]:
    """Fetch an abstract from OpenAlex by DOI, reconstructing when inverted-indexed.

    Args:
        doi: Digital Object Identifier to look up
        mailto: Contact email for OpenAlex User-Agent
        session: Optional requests.Session for backward compatibility

    Returns:
        Plain-text abstract or None if not available
    """
    if not doi:
        return None

    url = f"https://api.openalex.org/works/https://doi.org/{quote(doi)}?mailto={quote(mailto)}"

    # If session is provided, use old logic for compatibility
    if session:
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            abs_txt = data.get('abstract')
            if abs_txt:
                return strip_jats(abs_txt)
            ii = data.get('abstract_inverted_index')
            if ii:
                return _reconstruct_openalex(ii)
            return None
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
        if abs_txt:
            return strip_jats(abs_txt)
        ii = data.get('abstract_inverted_index')
        if ii:
            return _reconstruct_openalex(ii)
        return None
    except Exception:
        return None
