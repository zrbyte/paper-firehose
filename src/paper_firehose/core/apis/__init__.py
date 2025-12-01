"""
API client modules for fetching abstracts from various sources.

This package provides a unified interface for fetching abstracts from:
- Crossref
- Semantic Scholar
- OpenAlex
- PubMed
"""

from .crossref_client import get_crossref_abstract, search_crossref_abstract_by_title
from .semantic_scholar_client import get_semantic_scholar_abstract
from .openalex_client import get_openalex_abstract
from .pubmed_client import get_pubmed_abstract_by_doi

__all__ = [
    'get_crossref_abstract',
    'search_crossref_abstract_by_title',
    'get_semantic_scholar_abstract',
    'get_openalex_abstract',
    'get_pubmed_abstract_by_doi',
]
