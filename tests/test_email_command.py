"""Tests for commands.email_list and processors.emailer functionality.

Covers entry selection, email settings resolution, HTML extraction,
ranked entry rendering, PQA summary formatting in email, score badges,
and the dry-run email pipeline flow.
"""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from paper_firehose.commands.email_list import (
    _select_entries,
    _resolve_email_settings,
    _extract_ranked_entries_from_file,
)
from paper_firehose.processors.emailer import (
    EmailRenderer,
    SMTPSender,
    _fmt_score_badge,
)


# ---------------------------------------------------------------------------
# _fmt_score_badge
# ---------------------------------------------------------------------------

class TestScoreBadge:
    def test_renders_score(self):
        badge = _fmt_score_badge(0.857)
        assert "Score 0.85" in badge
        assert "span" in badge

    def test_none_returns_empty(self):
        assert _fmt_score_badge(None) == ""

    def test_zero_score(self):
        badge = _fmt_score_badge(0.0)
        assert "Score 0.00" in badge


# ---------------------------------------------------------------------------
# EmailRenderer.render_ranked_entries
# ---------------------------------------------------------------------------

class TestRenderRankedEntries:
    def test_renders_entries_with_scores(self):
        renderer = EmailRenderer()
        entries = [
            {"title": "Paper A", "link": "http://a.com", "authors": "Alice",
             "feed_name": "Nature", "abstract": "Abstract A", "summary": "",
             "rank_score": 0.9},
            {"title": "Paper B", "link": "http://b.com", "authors": "Bob",
             "feed_name": "Science", "abstract": "", "summary": "Summary B",
             "rank_score": 0.4},
        ]
        html = renderer.render_ranked_entries("Test Topic", entries)
        assert "Paper A" in html
        assert "Paper B" in html
        assert "Score 0.90" in html
        assert "Score 0.40" in html
        assert "Abstract A" in html
        assert "Summary B" in html

    def test_sorted_by_rank_desc(self):
        renderer = EmailRenderer()
        entries = [
            {"title": "Low", "link": "http://l", "authors": "", "feed_name": "",
             "abstract": "", "summary": "", "rank_score": 0.2},
            {"title": "High", "link": "http://h", "authors": "", "feed_name": "",
             "abstract": "", "summary": "", "rank_score": 0.9},
        ]
        html = renderer.render_ranked_entries("T", entries)
        assert html.index("High") < html.index("Low")

    def test_empty_entries(self):
        renderer = EmailRenderer()
        html = renderer.render_ranked_entries("T", [])
        assert html == ""

    def test_max_items_limit(self):
        renderer = EmailRenderer()
        entries = [
            {"title": f"Paper {i}", "link": f"http://{i}", "authors": "",
             "feed_name": "", "abstract": "", "summary": "", "rank_score": 0.5}
            for i in range(5)
        ]
        html = renderer.render_ranked_entries("T", entries, max_items=2)
        assert html.count("Paper") == 2

    def test_pqa_summary_block_in_email(self):
        renderer = EmailRenderer()
        pqa = json.dumps({"summary": "Key findings here.", "methods": "DFT+U."})
        entries = [
            {"title": "PQA Paper", "link": "http://p", "authors": "Eve",
             "feed_name": "PRL", "abstract": "Abstract", "summary": "",
             "rank_score": 0.8, "paper_qa_summary": pqa},
        ]
        html = renderer.render_ranked_entries("T", entries)
        assert "Key findings here." in html
        assert "DFT+U." in html
        assert "Fulltext summary" in html


# ---------------------------------------------------------------------------
# EmailRenderer._format_pqa_summary (for email)
# ---------------------------------------------------------------------------

class TestEmailFormatPqaSummary:
    def test_valid_json(self):
        renderer = EmailRenderer()
        pqa = json.dumps({"summary": "Main point.", "methods": "XRD."})
        result = renderer._format_pqa_summary(pqa)
        assert "Main point." in result
        assert "XRD." in result

    def test_double_encoded_json(self):
        renderer = EmailRenderer()
        inner = json.dumps({"summary": "Nested", "methods": "Nested M"})
        outer = json.dumps({"summary": inner, "methods": ""})
        result = renderer._format_pqa_summary(outer)
        assert "Nested" in result

    def test_none_returns_none(self):
        renderer = EmailRenderer()
        assert renderer._format_pqa_summary(None) is None
        assert renderer._format_pqa_summary("") is None

    def test_plain_text_fallback(self):
        renderer = EmailRenderer()
        result = renderer._format_pqa_summary("Not JSON")
        assert "Not JSON" in result


# ---------------------------------------------------------------------------
# _select_entries
# ---------------------------------------------------------------------------

class TestSelectEntries:
    def _make_db(self, tmp_path):
        """Create a DatabaseManager with some ranked entries."""
        import time
        from paper_firehose.core.database import DatabaseManager

        cfg = {
            "database": {
                "path": str(tmp_path / "papers.db"),
                "all_feeds_path": str(tmp_path / "all.db"),
                "history_path": str(tmp_path / "history.db"),
            }
        }
        db = DatabaseManager(cfg)
        for i, score in enumerate([0.9, 0.7, 0.5, 0.3, 0.1]):
            entry = {
                "title": f"Paper {i}",
                "link": f"http://example.com/{i}",
                "summary": f"Summary {i}",
                "published_parsed": time.strptime("2026-03-20", "%Y-%m-%d"),
            }
            eid = db.compute_entry_id(entry)
            db.save_current_entry(entry, "Feed", "topic", eid)
            db.update_entry_rank(eid, "topic", score)
        return db

    def test_no_filters(self, tmp_path):
        db = self._make_db(tmp_path)
        entries = _select_entries(db, "topic", only_with_summary=False,
                                  limit=None, min_rank_score=None)
        assert len(entries) == 5

    def test_min_rank_filter(self, tmp_path):
        db = self._make_db(tmp_path)
        entries = _select_entries(db, "topic", only_with_summary=False,
                                  limit=None, min_rank_score=0.5)
        assert len(entries) == 3
        assert all(e["rank_score"] >= 0.5 for e in entries)

    def test_limit(self, tmp_path):
        db = self._make_db(tmp_path)
        entries = _select_entries(db, "topic", only_with_summary=False,
                                  limit=2, min_rank_score=None)
        assert len(entries) == 2

    def test_sorted_by_rank_desc(self, tmp_path):
        db = self._make_db(tmp_path)
        entries = _select_entries(db, "topic", only_with_summary=False,
                                  limit=None, min_rank_score=None)
        scores = [e["rank_score"] for e in entries]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# _resolve_email_settings
# ---------------------------------------------------------------------------

class TestResolveEmailSettings:
    def test_valid_config(self):
        cfg = {
            "email": {
                "from": "sender@example.com",
                "to": "list@example.com",
                "smtp": {
                    "host": "smtp.example.com",
                    "port": 465,
                    "username": "user",
                },
            }
        }
        result = _resolve_email_settings(cfg)
        assert result["from"] == "sender@example.com"
        assert result["to"] == "list@example.com"

    def test_missing_smtp_raises(self):
        cfg = {"email": {"from": "a@b.com", "to": "c@d.com"}}
        with pytest.raises(RuntimeError, match="Missing email.smtp"):
            _resolve_email_settings(cfg)

    def test_missing_host_raises(self):
        cfg = {"email": {"smtp": {"port": 465, "username": "u"}}}
        with pytest.raises(RuntimeError, match="host"):
            _resolve_email_settings(cfg)

    def test_defaults_from_to_username(self):
        cfg = {
            "email": {
                "smtp": {"host": "h", "port": 465, "username": "user@mail.com"},
            }
        }
        result = _resolve_email_settings(cfg)
        assert result["from"] == "user@mail.com"


# ---------------------------------------------------------------------------
# _extract_ranked_entries_from_file
# ---------------------------------------------------------------------------

class TestExtractRankedEntries:
    def test_nonexistent_file(self):
        assert _extract_ranked_entries_from_file("/nonexistent/path.html") is None

    def test_extracts_body_content(self, tmp_path):
        html_file = tmp_path / "ranked.html"
        html_file.write_text(
            '<html><body><div class="content">Entries here</div></body></html>',
            encoding="utf-8",
        )
        result = _extract_ranked_entries_from_file(str(html_file))
        assert "Entries here" in result

    def test_extracts_ranked_entries_section(self, tmp_path):
        html_file = tmp_path / "ranked.html"
        html_file.write_text(
            '<html><body><p>Preamble</p><h2>Ranked Entries</h2><p>The entries</p></body></html>',
            encoding="utf-8",
        )
        result = _extract_ranked_entries_from_file(str(html_file))
        assert result.startswith("<h2>Ranked Entries</h2>")
        assert "The entries" in result
        assert "Preamble" not in result
