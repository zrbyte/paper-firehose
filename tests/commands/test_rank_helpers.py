import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import rank  # noqa: E402
from paper_firehose.core.text_utils import strip_accents, normalize_name, parse_name_parts, names_match  # noqa: E402
from paper_firehose.core import model_manager  # noqa: E402


def test_has_model_files_detects_config(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    assert model_manager.has_model_files(str(model_dir)) is True


def test_has_model_files_returns_false_for_empty_dir(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert model_manager.has_model_files(str(empty_dir)) is False


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
    assert strip_accents(text) == expected


def test_norm_name_removes_punctuation():
    assert normalize_name("José L. O'Connor") == "jose l o connor"


@pytest.mark.parametrize(
    "name,last,initials",
    [
        ("Doe, Jane A.", "doe", ["j", "a"]),
        ("Jane A Doe", "doe", ["j", "a"]),
        ("Single", "single", []),
    ],
)
def test_parse_name_parts_variations(name, last, initials):
    parsed_last, parsed_initials = parse_name_parts(name)
    assert parsed_last == last
    assert parsed_initials == initials


def test_names_match_allows_initials():
    assert names_match("Doe, Jane A.", "Jane Doe")
    assert names_match("García, M.", "Garcia, Maria")
    assert not names_match("Doe, Jane", "Smith, John")


def test_entry_has_preferred_author_matches_variant():
    entry = {"authors": "Jane A. Doe; John Smith"}
    preferred = ["Doe, J."]
    assert rank._entry_has_preferred_author(entry, preferred) is True
    assert (
        rank._entry_has_preferred_author(entry, ["Unrelated Author"]) is False
    )
