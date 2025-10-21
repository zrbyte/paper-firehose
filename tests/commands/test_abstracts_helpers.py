import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import abstracts  # noqa: E402


def test_strip_jats_removes_markup():
    raw = "<jats:p>Result &amp; More</jats:p>"
    assert abstracts._strip_jats(raw) == "Result & More"
    assert abstracts._strip_jats(None) is None


def test_clean_for_db_sanitizes_and_unwraps():
    raw = "Abstract: <b>Graphene advances</b>\u200b  "
    cleaned = abstracts._clean_for_db(raw)
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
    assert abstracts._find_doi_in_text(text) == expected


def test_extract_doi_from_raw_handles_nested_fields():
    raw = json.dumps(
        {
            "summary": "Contains doi:10.1111/xyz",
            "content": [{"value": "additional"}],
        }
    )
    assert abstracts._extract_doi_from_raw(raw) == "10.1111/xyz"


def test_reconstruct_openalex_inverted_index():
    inverted = {"hello": [0, 2], "world": [1]}
    result = abstracts._reconstruct_openalex(inverted)
    assert result == "hello world hello"


def test_try_publisher_apis_prefers_pubmed_for_pnas(monkeypatch):
    calls = []

    def fake_pubmed(doi, *, session):
        calls.append("pubmed")
        return "pubmed-abstract"

    def fake_semantic(doi, *, session):
        calls.append("semantic")
        return None

    def fake_openalex(doi, *, mailto, session):
        calls.append("openalex")
        return None

    monkeypatch.setattr(abstracts, "get_pubmed_abstract_by_doi", fake_pubmed)
    monkeypatch.setattr(abstracts, "get_semantic_scholar_abstract", fake_semantic)
    monkeypatch.setattr(abstracts, "get_openalex_abstract", fake_openalex)

    result = abstracts.try_publisher_apis(
        "10.1000/pnas",
        "PNAS Proceedings",
        "https://pnas.org/paper",
        mailto="test@example.com",
        session=None,
    )

    assert result == "pubmed-abstract"
    assert calls == ["pubmed"]


def test_try_publisher_apis_returns_semantic_scholar_first(monkeypatch):
    calls = []

    def fake_pubmed(doi, *, session):
        calls.append("pubmed")
        return None

    def fake_semantic(doi, *, session):
        calls.append("semantic")
        return "semantic-result"

    def fake_openalex(doi, *, mailto, session):
        calls.append("openalex")
        return "openalex-result"

    monkeypatch.setattr(abstracts, "get_pubmed_abstract_by_doi", fake_pubmed)
    monkeypatch.setattr(abstracts, "get_semantic_scholar_abstract", fake_semantic)
    monkeypatch.setattr(abstracts, "get_openalex_abstract", fake_openalex)

    result = abstracts.try_publisher_apis(
        "10.1000/test",
        "Generic Journal",
        "https://example.org/article",
        mailto="test@example.com",
        session=None,
    )

    assert result == "semantic-result"
    assert calls == ["semantic"]


def test_try_publisher_apis_falls_back_to_pubmed(monkeypatch):
    calls = []

    def fake_pubmed(doi, *, session):
        calls.append("pubmed")
        return "pubmed-final"

    def fake_semantic(doi, *, session):
        calls.append("semantic")
        return None

    def fake_openalex(doi, *, mailto, session):
        calls.append("openalex")
        return None

    monkeypatch.setattr(abstracts, "get_pubmed_abstract_by_doi", fake_pubmed)
    monkeypatch.setattr(abstracts, "get_semantic_scholar_abstract", fake_semantic)
    monkeypatch.setattr(abstracts, "get_openalex_abstract", fake_openalex)

    result = abstracts.try_publisher_apis(
        "10.1000/test",
        "Other Journal",
        "https://example.org/article",
        mailto="test@example.com",
        session=None,
    )

    assert result == "pubmed-final"
    assert calls == ["semantic", "openalex", "pubmed"]
