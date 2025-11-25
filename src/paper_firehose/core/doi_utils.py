"""Unified DOI extraction utilities.

Consolidates DOI extraction logic from database.py and abstracts.py into a
single, well-tested implementation.
"""

import re
import json
from typing import Optional, Dict, Any


# DOI regex from Crossref guidelines (simplified)
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)


def find_doi_in_text(text: Optional[str]) -> Optional[str]:
    """Search a text string for a DOI pattern.

    Strips common prefixes like 'doi:' before searching.

    Args:
        text: Text to search for DOI

    Returns:
        DOI string if found, None otherwise

    Examples:
        >>> find_doi_in_text("doi:10.1234/example")
        '10.1234/example'
        >>> find_doi_in_text("https://doi.org/10.1234/example")
        '10.1234/example'
        >>> find_doi_in_text("no doi here")
        None
    """
    if not text:
        return None

    text = str(text).strip()

    # Strip common prefixes
    if text.lower().startswith('doi:'):
        text = text[4:].strip()

    # Search for DOI pattern
    match = DOI_PATTERN.search(text)
    return match.group(0) if match else None


def extract_doi_from_entry(entry: Dict[str, Any]) -> Optional[str]:
    """Extract DOI from a feed entry dictionary.

    Searches multiple common fields where DOIs appear in RSS/Atom feeds,
    including Dublin Core, PRISM, and standard RSS fields.

    Args:
        entry: Feed entry dictionary (from feedparser or similar)

    Returns:
        DOI string if found, None otherwise

    Field priority order:
        1. Direct DOI fields (doi, dc_identifier, prism:doi, etc.)
        2. ID and link fields
        3. Summary/description fields
        4. Content arrays
        5. Links arrays
    """
    if not entry:
        return None

    # Priority 1: Direct DOI fields
    for key in [
        'doi',
        'dc_identifier', 'dc:identifier', 'dc.identifier', 'dcIdentifier',
        'prism:doi', 'prism_doi',
        'guid'
    ]:
        value = entry.get(key)
        doi = find_doi_in_text(value)
        if doi:
            return doi

    # Priority 2: ID and link fields
    for key in ['id', 'link']:
        value = entry.get(key)
        doi = find_doi_in_text(value)
        if doi:
            return doi

    # Priority 3: Summary/description fields
    doi = find_doi_in_text(entry.get('summary'))
    if doi:
        return doi

    # Check summary_detail if present
    summary_detail = entry.get('summary_detail') or {}
    if isinstance(summary_detail, dict):
        doi = find_doi_in_text(summary_detail.get('value'))
        if doi:
            return doi

    # Some feeds use 'description' instead of 'summary'
    doi = find_doi_in_text(entry.get('description'))
    if doi:
        return doi

    # Priority 4: Content arrays (check value or content fields)
    contents = entry.get('content') or []
    if isinstance(contents, list):
        for c in contents:
            if isinstance(c, dict):
                doi = find_doi_in_text(c.get('value') or c.get('content'))
                if doi:
                    return doi

    # Priority 5: Links arrays
    links = entry.get('links') or []
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict):
                href = link.get('href')
            else:
                href = str(link)
            doi = find_doi_in_text(href)
            if doi:
                return doi

    return None


def extract_doi_from_json(raw_json: Optional[str]) -> Optional[str]:
    """Extract DOI from a raw JSON string.

    Useful when dealing with stored feed entry JSON payloads.

    Args:
        raw_json: JSON string containing feed entry data

    Returns:
        DOI string if found, None otherwise
    """
    if not raw_json:
        return None

    try:
        obj = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None

    # Use the main extraction function
    return extract_doi_from_entry(obj)
