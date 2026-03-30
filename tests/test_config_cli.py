"""Tests for config and topic CLI subcommands."""

import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.cli import cli
import paper_firehose.core.config as core_config


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    """Set up a minimal config environment and return the config path."""
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"

    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(core_config, "_copy_tree", lambda src, dest: False)

    config_yaml = textwrap.dedent("""
        database:
          path: "papers.db"
          all_feeds_path: "all_feed_entries.db"
          history_path: "matched_entries_history.db"
        feeds:
          test_feed:
            name: "Test Feed"
            url: "https://example.com/feed"
            enabled: true
        defaults:
          rank_threshold: 0.3
          time_window_days: 365
    """).strip() + "\n"

    topic_yaml = textwrap.dedent("""
        name: "My Topic"
        description: "A test topic"
        feeds:
          - "test_feed"
        filter:
          pattern: "test"
          fields: ["title"]
        ranking:
          query: "test query"
    """).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")

    return str(config_path)


class TestConfigShow:
    def test_config_show(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "show"])
        assert result.exit_code == 0
        assert "database" in result.output
        assert "feeds" in result.output


class TestConfigGet:
    def test_get_simple_key(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "get", "defaults.rank_threshold"])
        assert result.exit_code == 0
        assert "0.3" in result.output

    def test_get_nested_key(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "get", "database.path"])
        assert result.exit_code == 0
        assert "papers.db" in result.output

    def test_get_missing_key(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "get", "nonexistent.key"])
        assert result.exit_code != 0


class TestConfigSet:
    def test_set_float(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "set", "defaults.rank_threshold", "0.25"])
        assert result.exit_code == 0
        assert "0.25" in result.output

        # Verify it was written
        result2 = runner.invoke(cli, ["--config", config_env, "config", "get", "defaults.rank_threshold"])
        assert "0.25" in result2.output

    def test_set_int(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "set", "defaults.time_window_days", "30"])
        assert result.exit_code == 0
        assert "30" in result.output

    def test_set_bool(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "set", "defaults.some_flag", "true"])
        assert result.exit_code == 0
        assert "True" in result.output


class TestConfigValidate:
    def test_validate_valid(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "config", "validate"])
        assert result.exit_code == 0
        assert "valid" in result.output.lower()


class TestTopicList:
    def test_list_topics(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "topic", "list"])
        assert result.exit_code == 0
        assert "test_topic" in result.output
        assert "My Topic" in result.output


class TestTopicShow:
    def test_show_topic(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "topic", "show", "test_topic"])
        assert result.exit_code == 0
        assert "My Topic" in result.output
        assert "ranking" in result.output

    def test_show_missing_topic(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "topic", "show", "nonexistent"])
        assert result.exit_code != 0


class TestTopicAdd:
    def test_add_from_template(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "topic", "add", "new_topic"])
        assert result.exit_code == 0
        assert "Created" in result.output

        # Verify the file exists and has the right name
        config_dir = Path(config_env).parent
        new_path = config_dir / "topics" / "new_topic.yaml"
        assert new_path.exists()
        data = yaml.safe_load(new_path.read_text())
        assert data["name"] == "new_topic"

    def test_add_from_existing(self, runner, config_env):
        result = runner.invoke(cli, [
            "--config", config_env, "topic", "add", "cloned_topic",
            "--from", "test_topic",
        ])
        assert result.exit_code == 0

        config_dir = Path(config_env).parent
        new_path = config_dir / "topics" / "cloned_topic.yaml"
        assert new_path.exists()
        data = yaml.safe_load(new_path.read_text())
        assert data["name"] == "cloned_topic"
        assert data["filter"]["pattern"] == "test"  # cloned from test_topic

    def test_add_duplicate_fails(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "topic", "add", "test_topic"])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_add_invalid_name(self, runner, config_env):
        result = runner.invoke(cli, ["--config", config_env, "topic", "add", "bad name!"])
        assert result.exit_code != 0
        assert "Invalid" in result.output
