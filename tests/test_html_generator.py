"""Tests for processors.html_generator.HTMLGenerator.

Exercises HTML rendering for filtered, ranked, and PQA-summarized views
using the same database-backed flow as a real pipeline run.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from paper_firehose.core.database import DatabaseManager
from paper_firehose.processors.html_generator import HTMLGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    cfg = {
        "database": {
            "path": str(tmp_path / "papers.db"),
            "all_feeds_path": str(tmp_path / "all_feed_entries.db"),
            "history_path": str(tmp_path / "matched_entries_history.db"),
        }
    }
    return DatabaseManager(cfg)


def _insert_entry(db, title, topic, rank_score=None, abstract=None, pqa_summary=None):
    entry = {
        "title": title,
        "link": f"http://example.com/{title.replace(' ', '_')}",
        "summary": f"Summary of {title}",
        "authors": [{"name": "Author A"}, {"name": "Author B"}],
        "published_parsed": time.strptime("2026-03-20", "%Y-%m-%d"),
    }
    eid = db.compute_entry_id(entry)
    db.save_current_entry(entry, "Test Feed", topic, eid)

    if rank_score is not None:
        db.update_entry_rank(eid, topic, rank_score)

    if abstract or pqa_summary:
        with db.get_connection("current", row_factory=False) as conn:
            if abstract:
                conn.execute("UPDATE entries SET abstract = ? WHERE id = ? AND topic = ?",
                             (abstract, eid, topic))
            if pqa_summary:
                conn.execute("UPDATE entries SET paper_qa_summary = ? WHERE id = ? AND topic = ?",
                             (pqa_summary, eid, topic))
    return eid


# ---------------------------------------------------------------------------
# process_text
# ---------------------------------------------------------------------------

class TestProcessText:
    def test_empty_string(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        assert gen.process_text("") == ""
        assert gen.process_text(None) == ""

    def test_preserves_latex(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        result = gen.process_text("$\\alpha$ < 0.5")
        assert "$" in result
        assert "\\" in result

    def test_plain_text_with_angle_brackets(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        # process_text escapes HTML then unescapes < > & for LaTeX preservation
        result = gen.process_text("a < b > c & d")
        assert "a < b > c & d" in result


# ---------------------------------------------------------------------------
# _format_pqa_summary
# ---------------------------------------------------------------------------

class TestFormatPqaSummary:
    def test_valid_json(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        pqa = json.dumps({"summary": "Good paper.", "methods": "DFT calculations."})
        html = gen._format_pqa_summary(pqa)
        assert "Good paper." in html
        assert "DFT calculations." in html
        assert "Summary" in html
        assert "Methods" in html

    def test_double_encoded_json(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        inner = json.dumps({"summary": "Nested summary", "methods": "Nested methods"})
        outer = json.dumps({"summary": inner, "methods": ""})
        html = gen._format_pqa_summary(outer)
        assert "Nested summary" in html

    def test_plain_text_fallback(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        html = gen._format_pqa_summary("Just a plain text summary.")
        assert "Just a plain text summary." in html
        assert "PDF Summary" in html

    def test_empty_returns_no_summary(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        html = gen._format_pqa_summary("")
        assert "No summary available" in html


# ---------------------------------------------------------------------------
# _format_llm_summary
# ---------------------------------------------------------------------------

class TestFormatLlmSummary:
    def test_valid_json_with_sections(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        data = json.dumps({
            "summary": "Main findings.",
            "topical_relevance": "Highly relevant.",
            "novelty_impact": "Novel approach."
        })
        html = gen._format_llm_summary(data)
        assert "Main findings." in html
        assert "Highly relevant." in html
        assert "Novel approach." in html

    def test_fallback_on_invalid_json(self):
        gen = HTMLGenerator.__new__(HTMLGenerator)
        html = gen._format_llm_summary("Not JSON at all")
        assert "Not JSON at all" in html
        assert "LLM Summary" in html


# ---------------------------------------------------------------------------
# Full HTML generation from database
# ---------------------------------------------------------------------------

class TestHTMLFromDatabase:
    def test_filtered_html_generation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        _insert_entry(db, "Graphene transport", "demo")

        gen = HTMLGenerator()
        out = str(tmp_path / "filtered.html")
        gen.generate_html_from_database(db, "demo", out, heading="Demo Topic")

        html = Path(out).read_text()
        assert "Graphene transport" in html
        assert "Author A" in html

    def test_filtered_html_no_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        gen = HTMLGenerator()
        out = str(tmp_path / "empty.html")
        gen.generate_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "No new entries found" in html

    def test_ranked_html_has_score_badge(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        _insert_entry(db, "High-score paper", "demo", rank_score=0.92)
        _insert_entry(db, "Low-score paper", "demo", rank_score=0.31)

        gen = HTMLGenerator()
        out = str(tmp_path / "ranked.html")
        gen.generate_ranked_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "Score 0.92" in html
        assert "Score 0.31" in html
        # High score should appear before low score
        assert html.index("High-score paper") < html.index("Low-score paper")

    def test_ranked_html_no_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        gen = HTMLGenerator()
        out = str(tmp_path / "ranked_empty.html")
        gen.generate_ranked_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "No ranked entries" in html

    def test_pqa_summary_html_with_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        pqa = json.dumps({"summary": "DFT study of perovskites.", "methods": "PBE+U."})
        _insert_entry(db, "Perovskite paper", "demo", rank_score=0.85, pqa_summary=pqa)

        gen = HTMLGenerator()
        out = str(tmp_path / "summary.html")
        gen.generate_pqa_summarized_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "DFT study of perovskites." in html
        assert "PBE+U." in html
        assert "PDF summary" in html  # tag label

    def test_pqa_summary_html_without_summary_shows_abstract(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        _insert_entry(db, "No-PQA paper", "demo", rank_score=0.6, abstract="The abstract text here.")

        gen = HTMLGenerator()
        out = str(tmp_path / "summary2.html")
        gen.generate_pqa_summarized_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "The abstract text here." in html
        assert "Ranked" in html  # tag label for non-PQA entries

    def test_pqa_summary_html_entry_count(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        pqa = json.dumps({"summary": "s", "methods": "m"})
        _insert_entry(db, "Paper A", "demo", rank_score=0.9, pqa_summary=pqa)
        _insert_entry(db, "Paper B", "demo", rank_score=0.7)

        gen = HTMLGenerator()
        out = str(tmp_path / "count.html")
        gen.generate_pqa_summarized_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "2 ranked entries" in html
        assert "1 with PDF summaries" in html

    def test_abstract_preferred_over_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        db = _make_db(tmp_path)
        _insert_entry(db, "Paper", "demo", rank_score=0.5, abstract="Full abstract.")

        gen = HTMLGenerator()
        out = str(tmp_path / "pref.html")
        gen.generate_ranked_html_from_database(db, "demo", out)

        html = Path(out).read_text()
        assert "Full abstract." in html
