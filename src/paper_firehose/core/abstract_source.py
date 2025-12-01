"""
Abstract source interface using Python Protocol for structural subtyping.

Provides a unified interface for fetching abstracts from various sources
(Crossref, Semantic Scholar, OpenAlex, PubMed) with fallback support.
"""

from __future__ import annotations

from typing import Protocol, Optional, runtime_checkable

import requests


@runtime_checkable
class AbstractSource(Protocol):
    """Protocol defining the interface for abstract fetching sources.

    All abstract sources should implement fetch_abstract() method that
    accepts DOI, title, and optional parameters, returning the abstract
    text or None if not found.
    """

    def fetch_abstract(
        self,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        mailto: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> Optional[str]:
        """Fetch abstract from this source.

        Args:
            doi: Digital Object Identifier (optional)
            title: Paper title (optional)
            mailto: Contact email for polite API usage (optional)
            session: requests.Session for connection pooling (optional)

        Returns:
            Abstract text or None if not found
        """
        ...


class CrossrefSource:
    """Crossref abstract source with DOI lookup and title search."""

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    def fetch_abstract(
        self,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        mailto: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> Optional[str]:
        """Fetch abstract from Crossref by DOI or title."""
        from ..core.apis import get_crossref_abstract, search_crossref_abstract_by_title

        if not mailto:
            mailto = "noreply@example.com"

        # Try DOI first if available
        if doi:
            result = get_crossref_abstract(
                doi, mailto=mailto, max_retries=self.max_retries, session=session
            )
            if result:
                return result

        # Fall back to title search
        if title:
            return search_crossref_abstract_by_title(
                title, mailto=mailto, max_retries=self.max_retries - 1, session=session
            )

        return None


class SemanticScholarSource:
    """Semantic Scholar abstract source (DOI-based lookup)."""

    def fetch_abstract(
        self,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        mailto: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> Optional[str]:
        """Fetch abstract from Semantic Scholar by DOI."""
        from ..core.apis import get_semantic_scholar_abstract

        if doi:
            return get_semantic_scholar_abstract(doi, session=session)
        return None


class OpenAlexSource:
    """OpenAlex abstract source with inverted-index reconstruction."""

    def fetch_abstract(
        self,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        mailto: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> Optional[str]:
        """Fetch abstract from OpenAlex by DOI."""
        from ..core.apis import get_openalex_abstract

        if not mailto:
            mailto = "noreply@example.com"

        if doi:
            return get_openalex_abstract(doi, mailto=mailto, session=session)
        return None


class PubMedSource:
    """PubMed abstract source (DOI-based lookup via ESearch + EFetch)."""

    def fetch_abstract(
        self,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        mailto: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> Optional[str]:
        """Fetch abstract from PubMed by DOI."""
        from ..core.apis import get_pubmed_abstract_by_doi

        if doi:
            return get_pubmed_abstract_by_doi(doi, session=session)
        return None


def get_default_sources() -> list[AbstractSource]:
    """Return default list of abstract sources in priority order.

    Order: Crossref (most comprehensive), Semantic Scholar, OpenAlex, PubMed.

    Returns:
        List of AbstractSource instances
    """
    return [
        CrossrefSource(),
        SemanticScholarSource(),
        OpenAlexSource(),
        PubMedSource(),
    ]


def get_biomedical_sources() -> list[AbstractSource]:
    """Return abstract sources optimized for biomedical papers.

    Order: PubMed (best for PNAS/biomedical), Crossref, Semantic Scholar, OpenAlex.

    Returns:
        List of AbstractSource instances
    """
    return [
        PubMedSource(),
        CrossrefSource(),
        SemanticScholarSource(),
        OpenAlexSource(),
    ]
