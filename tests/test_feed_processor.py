"""Tests for processors.feed_processor.FeedProcessor.

Exercises regex filtering, pattern matching, feed deduplication, and entry
storage using the same data flow as a real filter run, but with local feeds.
"""

import re
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paper_firehose.core.config import ConfigManager
from paper_firehose.core.database import DatabaseManager
from paper_firehose.processors.feed_processor import FeedProcessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env(tmp_path, topic_yaml=None, config_yaml=None, feed_uri=None):
    """Bootstrap a config + DB in tmp_path; return (config_manager, db_manager)."""
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)

    feed_path = Path(__file__).parent / "fixtures" / "sample_feed.xml"
    uri = feed_uri or feed_path.resolve().as_uri()

    if config_yaml is None:
        config_yaml = textwrap.dedent(f"""
            database:
              path: "{tmp_path}/papers.db"
              all_feeds_path: "{tmp_path}/all_feed_entries.db"
              history_path: "{tmp_path}/matched_entries_history.db"
            feeds:
              local_feed:
                name: "Local Test Feed"
                url: "{uri}"
                enabled: true
            priority_journals: []
            defaults:
              time_window_days: 365
        """).strip() + "\n"

    if topic_yaml is None:
        topic_yaml = textwrap.dedent("""
            name: "test_topic"
            description: "Test topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "graphene"
              fields: ["title", "summary"]
            ranking:
              query: "graphene materials"
            output:
              archive: true
        """).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")

    from paper_firehose.core import config as core_config
    with patch.object(core_config, "_copy_tree", return_value=False):
        cfg_mgr = ConfigManager(str(config_path))

    cfg = cfg_mgr.load_config()
    db = DatabaseManager(cfg)
    return cfg_mgr, db


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

class TestPatternMatching:
    def test_matches_title(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("graphene", re.IGNORECASE)
        entry = {"title": "Graphene nanoribbon study", "summary": "Unrelated."}
        assert proc._matches_pattern(entry, regex, ["title"]) is True

    def test_matches_summary(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("graphene", re.IGNORECASE)
        entry = {"title": "Materials study", "summary": "New results on graphene oxide."}
        assert proc._matches_pattern(entry, regex, ["summary"]) is True

    def test_no_match(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("graphene", re.IGNORECASE)
        entry = {"title": "Silicon solar cells", "summary": "Photovoltaic efficiency."}
        assert proc._matches_pattern(entry, regex, ["title", "summary"]) is False

    def test_matches_author_field(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("Novoselov", re.IGNORECASE)
        entry = {"title": "2D materials", "summary": "s",
                 "authors": [{"name": "K. S. Novoselov"}]}
        assert proc._matches_pattern(entry, regex, ["authors"]) is True

    def test_case_insensitive_match(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("GRAPHENE", re.IGNORECASE)
        entry = {"title": "graphene", "summary": ""}
        assert proc._matches_pattern(entry, regex, ["title"]) is True

    def test_complex_regex_pattern(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile(r"perovsk|halide.*solar|CsPb", re.IGNORECASE)
        assert proc._matches_pattern({"title": "CsPbBr3 quantum dots", "summary": ""},
                                     regex, ["title"]) is True
        assert proc._matches_pattern({"title": "halide solar cell", "summary": ""},
                                     regex, ["title"]) is True
        assert proc._matches_pattern({"title": "silicon wafer", "summary": ""},
                                     regex, ["title"]) is False


# ---------------------------------------------------------------------------
# apply_filters — end-to-end with local feed
# ---------------------------------------------------------------------------

class TestApplyFilters:
    def test_filters_local_feed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)

        # Fetch real entries from the local sample feed
        entries_per_feed = proc.fetch_feeds("test_topic")

        matched = proc.apply_filters(entries_per_feed, "test_topic")
        titles = [e["title"] for e in matched]
        assert "Graphene breakthroughs in materials science" in titles
        assert "Other topic unrelated to filters" not in titles

    def test_matched_entries_saved_to_history(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.apply_filters(entries_per_feed, "test_topic")

        with db.get_connection("history") as conn:
            count = conn.execute("SELECT COUNT(*) FROM matched_entries").fetchone()[0]
        assert count >= 1

    def test_matched_entries_saved_to_current(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.apply_filters(entries_per_feed, "test_topic")

        rows = db.get_current_entries(topic="test_topic")
        assert len(rows) >= 1
        assert all(r["status"] == "filtered" for r in rows)

    def test_invalid_regex_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        topic_yaml = textwrap.dedent("""
            name: "test_topic"
            description: "Bad regex topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "[invalid(regex"
              fields: ["title"]
            ranking:
              query: "test"
        """).strip() + "\n"
        cfg_mgr, db = _make_env(tmp_path, topic_yaml=topic_yaml)
        proc = FeedProcessor(db, cfg_mgr)
        entries = {"local_feed": [{"title": "test", "link": "http://x", "id": "1"}]}
        matched = proc.apply_filters(entries, "test_topic")
        assert matched == []


# ---------------------------------------------------------------------------
# save_all_entries_to_dedup_db
# ---------------------------------------------------------------------------

class TestSaveAllEntries:
    def test_entries_persisted_to_dedup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)

        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.save_all_entries_to_dedup_db(entries_per_feed)

        with db.get_connection("all_feeds") as conn:
            count = conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0]
        # sample_feed.xml has 2 entries
        assert count >= 2

    def test_dedup_prevents_recount(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)

        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.save_all_entries_to_dedup_db(entries_per_feed)

        # Second fetch — titles already in dedup DB
        entries_per_feed2 = proc.fetch_feeds("test_topic")
        total_new = sum(len(v) for v in entries_per_feed2.values())
        assert total_new == 0
