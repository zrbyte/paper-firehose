import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.core.text_utils import strip_jats, clean_abstract_for_db  # noqa: E402
from paper_firehose.core.doi_utils import find_doi_in_text, extract_doi_from_json  # noqa: E402
from paper_firehose.core.apis import openalex_client  # noqa: E402
from paper_firehose.processors import abstract_fetcher  # noqa: E402
from paper_firehose.core.apis import (  # noqa: E402
    get_pubmed_abstract_by_doi,
    get_semantic_scholar_abstract,
    get_openalex_abstract,
)


def test_strip_jats_removes_markup():
    raw = "<jats:p>Result &amp; More</jats:p>"
    assert strip_jats(raw) == "Result & More"
    assert strip_jats(None) is None


def test_clean_for_db_sanitizes_and_unwraps():
    raw = "Abstract: <b>Graphene advances</b>\u200b  "
    cleaned = clean_abstract_for_db(raw)
    assert cleaned == "Graphene advances"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("doi:10.1234/ABC-123", "10.1234/ABC-123"),
        ("See https://doi.org/10.5678/foo.bar", "10.5678/foo.bar"),
        (None, None),
        ("No DOI present", None),
    ],
)
def test_find_doi_in_text(text, expected):
    assert find_doi_in_text(text) == expected


def test_extract_doi_from_raw_handles_nested_fields():
    raw = json.dumps(
        {
            "summary": "Contains doi:10.1111/xyz",
            "content": [{"value": "additional"}],
        }
    )
    assert extract_doi_from_json(raw) == "10.1111/xyz"


def test_reconstruct_openalex_inverted_index():
    inverted = {"hello": [0, 2], "world": [1]}
    result = openalex_client._reconstruct_openalex(inverted)
    assert result == "hello world hello"


def test_try_publisher_apis_prefers_pubmed_for_pnas(monkeypatch):
    class FakePubMedSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return "pubmed-abstract"

    class FakeCrossrefSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    class FakeSemanticScholarSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    class FakeOpenAlexSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    # Mock biomedical sources to return PubMed first - patch at point of use
    def fake_biomedical_sources():
        return [FakePubMedSource(), FakeCrossrefSource(), FakeSemanticScholarSource(), FakeOpenAlexSource()]

    monkeypatch.setattr(abstract_fetcher, "get_biomedical_sources", fake_biomedical_sources)

    result = abstract_fetcher.try_publisher_apis(
        "10.1000/pnas",
        "PNAS Proceedings",
        "https://pnas.org/paper",
        mailto="test@example.com",
        session=None,
    )

    assert result == "pubmed-abstract"


def test_try_publisher_apis_returns_semantic_scholar_first(monkeypatch):
    class FakeCrossrefSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    class FakeSemanticScholarSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return "semantic-result"

    class FakeOpenAlexSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return "openalex-result"

    class FakePubMedSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    # Mock default sources to return Crossref, Semantic Scholar, OpenAlex, PubMed in order
    def fake_default_sources():
        return [FakeCrossrefSource(), FakeSemanticScholarSource(), FakeOpenAlexSource(), FakePubMedSource()]

    monkeypatch.setattr(abstract_fetcher, "get_default_sources", fake_default_sources)

    result = abstract_fetcher.try_publisher_apis(
        "10.1000/test",
        "Generic Journal",
        "https://example.org/article",
        mailto="test@example.com",
        session=None,
    )

    assert result == "semantic-result"


def test_try_publisher_apis_falls_back_to_pubmed(monkeypatch):
    class FakeCrossrefSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    class FakeSemanticScholarSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    class FakeOpenAlexSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return None

    class FakePubMedSource:
        def fetch_abstract(self, doi=None, title=None, mailto=None, session=None):
            return "pubmed-final"

    # Mock default sources to return all sources with PubMed last
    def fake_default_sources():
        return [FakeCrossrefSource(), FakeSemanticScholarSource(), FakeOpenAlexSource(), FakePubMedSource()]

    monkeypatch.setattr(abstract_fetcher, "get_default_sources", fake_default_sources)

    result = abstract_fetcher.try_publisher_apis(
        "10.1000/test",
        "Other Journal",
        "https://example.org/article",
        mailto="test@example.com",
        session=None,
    )

    assert result == "pubmed-final"
