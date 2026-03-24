"""Tests for core.command_context.CommandContext.

Exercises initialization, topic resolution, and config access helpers
using realistic config setups.
"""

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from paper_firehose.core import config as core_config
from paper_firehose.core.command_context import CommandContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup(tmp_path, config_yaml=None, topic_yaml=None):
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)

    if config_yaml is None:
        config_yaml = textwrap.dedent(f"""
            database:
              path: "{tmp_path}/papers.db"
              all_feeds_path: "{tmp_path}/all_feed_entries.db"
              history_path: "{tmp_path}/matched_entries_history.db"
            feeds:
              local_feed:
                name: "Test Feed"
                url: "http://example.com/rss"
                enabled: true
            priority_journals: []
            defaults:
              time_window_days: 365
              rank_threshold: 0.3
              abstracts:
                mailto: "test@example.com"
        """).strip() + "\n"

    if topic_yaml is None:
        topic_yaml = textwrap.dedent("""
            name: "demo"
            feeds:
              - "local_feed"
            filter:
              pattern: "graphene"
              fields: ["title"]
            ranking:
              query: "graphene"
        """).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "demo.yaml").write_text(topic_yaml, encoding="utf-8")
    return str(config_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCommandContextInit:
    def test_successful_init(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        assert ctx.config is not None
        assert ctx.db is not None

    def test_invalid_config_raises(self, tmp_path):
        config_dir = tmp_path / "config"
        topics_dir = config_dir / "topics"
        topics_dir.mkdir(parents=True)

        bad_yaml = "feeds: {}\n"  # missing database section
        config_path = config_dir / "config.yaml"
        config_path.write_text(bad_yaml, encoding="utf-8")

        with patch.object(core_config, "_copy_tree", return_value=False):
            with pytest.raises(ValueError, match="Invalid configuration"):
                CommandContext(str(config_path))

    def test_context_manager(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            with CommandContext(path) as ctx:
                assert ctx.db is not None


class TestTopicResolution:
    def test_single_topic(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        assert ctx.get_topics("demo") == ["demo"]

    def test_all_topics(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        topics = ctx.get_topics(None)
        assert "demo" in topics

    def test_load_topic_config(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        cfg = ctx.load_topic_config("demo")
        assert cfg["name"] == "demo"
        assert cfg["filter"]["pattern"] == "graphene"


class TestConfigDefaults:
    def test_get_default(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        assert ctx.get_default("rank_threshold") == 0.3
        assert ctx.get_default("missing_key", 42) == 42

    def test_get_nested_default(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        assert ctx.get_nested_default("abstracts", "mailto") == "test@example.com"
        assert ctx.get_nested_default("abstracts", "missing", default="fallback") == "fallback"

    def test_get_nested_default_missing_section(self, tmp_path):
        with patch.object(core_config, "_copy_tree", return_value=False):
            path = _setup(tmp_path)
            ctx = CommandContext(path)
        assert ctx.get_nested_default("nonexistent", "key", default="x") == "x"
