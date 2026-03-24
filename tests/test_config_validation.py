"""Tests for config validation and edge cases in core.config.ConfigManager.

Supplements test_config_manager.py with validation logic, error handling,
and edge case coverage.
"""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from paper_firehose.core import config as core_config
from paper_firehose.core.config import ConfigManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path, config_text, topic_text=None, topic_name="test_topic"):
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(textwrap.dedent(config_text).strip() + "\n", encoding="utf-8")
    if topic_text:
        (topics_dir / f"{topic_name}.yaml").write_text(
            textwrap.dedent(topic_text).strip() + "\n", encoding="utf-8"
        )
    return str(config_path)


def _base_config():
    return """
        database:
          path: "papers.db"
          all_feeds_path: "all_feed_entries.db"
          history_path: "matched_entries_history.db"
        feeds:
          local_feed:
            name: "Test Feed"
            url: "http://example.com/rss"
            enabled: true
        priority_journals: []
    """


def _base_topic():
    return """
        name: "test_topic"
        feeds:
          - "local_feed"
        filter:
          pattern: "graphene"
          fields: ["title"]
        ranking:
          query: "graphene"
    """


# ---------------------------------------------------------------------------
# Valid configurations
# ---------------------------------------------------------------------------

class TestValidConfig:
    def test_valid_config_passes(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), _base_topic())
            mgr = ConfigManager(path)
        assert mgr.validate_config() is True

    def test_enabled_feeds(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config())
            mgr = ConfigManager(path)
        feeds = mgr.get_enabled_feeds()
        assert "local_feed" in feeds

    def test_disabled_feed_excluded(self, tmp_path):
        cfg = """
            database:
              path: "papers.db"
              all_feeds_path: "all_feed_entries.db"
              history_path: "matched_entries_history.db"
            feeds:
              active:
                name: "Active"
                url: "http://a.com"
                enabled: true
              inactive:
                name: "Inactive"
                url: "http://b.com"
                enabled: false
            priority_journals: []
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, cfg)
            mgr = ConfigManager(path)
        feeds = mgr.get_enabled_feeds()
        assert "active" in feeds
        assert "inactive" not in feeds


# ---------------------------------------------------------------------------
# Missing required sections
# ---------------------------------------------------------------------------

class TestMissingSections:
    def test_missing_database_section(self, tmp_path):
        cfg = """
            feeds:
              f:
                name: "F"
                url: "http://x"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, cfg)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_missing_feeds_section(self, tmp_path):
        cfg = """
            database:
              path: "papers.db"
              all_feeds_path: "all_feed_entries.db"
              history_path: "matched_entries_history.db"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, cfg)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_missing_db_key(self, tmp_path):
        cfg = """
            database:
              path: "papers.db"
              all_feeds_path: "all_feed_entries.db"
            feeds:
              f:
                name: "F"
                url: "http://x"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, cfg)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False


# ---------------------------------------------------------------------------
# Topic validation
# ---------------------------------------------------------------------------

class TestTopicValidation:
    def test_topic_missing_filter(self, tmp_path):
        topic = """
            name: "bad"
            feeds:
              - "local_feed"
            ranking:
              query: "test"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_topic_missing_feeds(self, tmp_path):
        topic = """
            name: "bad"
            filter:
              pattern: "test"
            ranking:
              query: "test"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_topic_references_unknown_feed(self, tmp_path):
        topic = """
            name: "bad"
            feeds:
              - "nonexistent_feed"
            filter:
              pattern: "test"
              fields: ["title"]
            ranking:
              query: "test"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_topic_invalid_regex(self, tmp_path):
        topic = """
            name: "bad"
            feeds:
              - "local_feed"
            filter:
              pattern: "[unclosed"
              fields: ["title"]
            ranking:
              query: "test"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_topic_empty_pattern(self, tmp_path):
        topic = """
            name: "bad"
            feeds:
              - "local_feed"
            filter:
              pattern: ""
              fields: ["title"]
            ranking:
              query: "test"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False


# ---------------------------------------------------------------------------
# Ranking config validation
# ---------------------------------------------------------------------------

class TestRankingValidation:
    def test_negative_queries_must_be_list_of_strings(self, tmp_path):
        topic = """
            name: "test_topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "graphene"
              fields: ["title"]
            ranking:
              query: "graphene"
              negative_queries: "not-a-list"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_preferred_authors_must_be_list(self, tmp_path):
        topic = """
            name: "test_topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "graphene"
              fields: ["title"]
            ranking:
              query: "graphene"
              preferred_authors: 42
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_valid_negative_queries_accepted(self, tmp_path):
        topic = """
            name: "test_topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "graphene"
              fields: ["title"]
            ranking:
              query: "graphene"
              negative_queries:
                - "machine learning"
                - "neural network"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), topic)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is True


# ---------------------------------------------------------------------------
# Priority journals validation
# ---------------------------------------------------------------------------

class TestPriorityJournalsValidation:
    def test_priority_journals_must_be_list(self, tmp_path):
        cfg = """
            database:
              path: "papers.db"
              all_feeds_path: "all_feed_entries.db"
              history_path: "matched_entries_history.db"
            feeds:
              f:
                name: "F"
                url: "http://x"
            priority_journals: "not-a-list"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, cfg)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False

    def test_priority_journal_boost_must_be_number(self, tmp_path):
        cfg = """
            database:
              path: "papers.db"
              all_feeds_path: "all_feed_entries.db"
              history_path: "matched_entries_history.db"
            feeds:
              f:
                name: "F"
                url: "http://x"
            priority_journals: []
            priority_journal_boost: "high"
        """
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, cfg)
            mgr = ConfigManager(path)
        assert mgr.validate_config() is False


# ---------------------------------------------------------------------------
# Topic path resolution
# ---------------------------------------------------------------------------

class TestTopicResolution:
    def test_missing_topic_raises(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config())
            mgr = ConfigManager(path)
        with pytest.raises(FileNotFoundError):
            mgr.load_topic_config("nonexistent")

    def test_available_topics_lists_yaml_files(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), _base_topic())
            mgr = ConfigManager(path)
        topics = mgr.get_available_topics()
        assert "test_topic" in topics

    def test_config_caching(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _write_config(tmp_path, _base_config(), _base_topic())
            mgr = ConfigManager(path)
        cfg1 = mgr.load_config()
        cfg2 = mgr.load_config()
        assert cfg1 is cfg2  # same object, cached
