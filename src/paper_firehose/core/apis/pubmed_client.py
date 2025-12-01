"""
PubMed API client for fetching paper abstracts.

PubMed (NCBI) provides access to biomedical literature abstracts through the
E-utilities API (ESearch and EFetch).
"""

from __future__ import annotations

from typing import Optional
import xml.etree.ElementTree as ET

import requests

from ..http_client import RetryableHTTPClient
from ..text_utils import strip_jats


def get_pubmed_abstract_by_doi(
    doi: str,
    *,
    session: Optional[requests.Session] = None
) -> Optional[str]:
    """Look up a DOI in PubMed and return the combined abstract text if available.

    Uses ESearch to find PMID by DOI, then EFetch to retrieve the abstract XML.

    Args:
        doi: Digital Object Identifier to look up
        session: Optional requests.Session for backward compatibility

    Returns:
        Plain-text abstract or None if not available
    """
    if not doi:
        return None

    # If session is provided, use old logic for compatibility
    if session:
        try:
            # ESearch for PMID by DOI
            es = session.get(
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
            ef = session.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={"db": "pubmed", "id": pmid, "retmode": "xml"},
                timeout=15,
            )
            ef.raise_for_status()
            root = ET.fromstring(ef.text)
            texts = []
            for at in root.findall('.//AbstractText'):
                texts.append(''.join(at.itertext()).strip())
            return strip_jats(' '.join(t for t in texts if t)) if texts else None
        except Exception:
            return None

    # Use new RetryableHTTPClient for better retry logic
    try:
        client = RetryableHTTPClient(rps=0.33, max_retries=3)  # PubMed rate limit: 3 req/sec

        # ESearch for PMID by DOI
        es = client.get_with_retry(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": f"{doi}[DOI]", "retmode": "json"}
        )
        if es is None:
            return None

        idlist = (es.json().get('esearchresult') or {}).get('idlist') or []
        if not idlist:
            return None
        pmid = idlist[0]

        # EFetch to get abstract XML
        ef = client.get_with_retry(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pubmed", "id": pmid, "retmode": "xml"}
        )
        if ef is None:
            return None

        root = ET.fromstring(ef.text)
        texts = []
        for at in root.findall('.//AbstractText'):
            texts.append(''.join(at.itertext()).strip())
        return strip_jats(' '.join(t for t in texts if t)) if texts else None
    except Exception:
        return None
