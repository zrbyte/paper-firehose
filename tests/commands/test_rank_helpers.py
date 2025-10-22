import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import rank  # noqa: E402

def test_ensure_local_model_maps_legacy_alias():
    assert rank._ensure_local_model("all-MiniLM-L6-v2") == "BAAI/bge-small-en-v1.5"


def test_ensure_local_model_passthrough_for_custom():
    assert rank._ensure_local_model("custom/model") == "custom/model"


def test_build_entry_text_strips_whitespace():
    entry = {"title": "  Graphene Insights  "}
    assert rank._build_entry_text(entry) == "Graphene Insights"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Café", "Cafe"),
        ("naïve façade", "naive facade"),
        ("plain", "plain"),
    ],
)
def test_strip_accents(text, expected):
    assert rank._strip_accents(text) == expected


def test_norm_name_removes_punctuation():
    assert rank._norm_name("José L. O'Connor") == "jose l o connor"


@pytest.mark.parametrize(
    "name,last,initials",
    [
        ("Doe, Jane A.", "doe", ["j", "a"]),
        ("Jane A Doe", "doe", ["j", "a"]),
        ("Single", "single", []),
    ],
)
def test_parse_name_parts_variations(name, last, initials):
    parsed_last, parsed_initials = rank._parse_name_parts(name)
    assert parsed_last == last
    assert parsed_initials == initials


def test_names_match_allows_initials():
    assert rank._names_match("Doe, Jane A.", "Jane Doe")
    assert rank._names_match("García, M.", "Garcia, Maria")
    assert not rank._names_match("Doe, Jane", "Smith, John")


def test_entry_has_preferred_author_matches_variant():
    entry = {"authors": "Jane A. Doe; John Smith"}
    preferred = ["Doe, J."]
    assert rank._entry_has_preferred_author(entry, preferred) is True
    assert (
        rank._entry_has_preferred_author(entry, ["Unrelated Author"]) is False
    )
